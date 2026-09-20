# Kobo delivery over Google Drive

Why Editions delivered to Google Drive arrive unreadable on a Kobo, why that is not this
pipeline's fault, and what can actually be done about it. Investigated on a Kobo Libra Colour
running firmware 5.18.270971, on 2026-09-06 and again in detail on 2026-09-20.

Reported upstream as [kobolabs/epub-spec#75](https://github.com/kobolabs/epub-spec/issues/75).

## The defect

Kobo's firmware opens the destination file before checking the HTTP status, writes the body of
a failed Drive request into it, refreshes its OAuth access token, retries, and then **appends**
the real bytes instead of truncating first. The Edition arrives intact behind a 507-byte Google
`401 authError` JSON body.

OCF requires the `mimetype` entry to be the first entry in the ZIP at offset 0. In a damaged
download it sits at offset 507, so the container is invalid:

```
first entry: mimetype | header_offset: 507      <- must be 0
14 entries, testzip() -> None                   <- payload fully intact
payload sha256 matches the object on Drive, exactly
```

**The account is not logged out.** The complete EPUB follows the error body, so the refresh and
the retry both succeed. That misreading is what cost two weeks.

## Why it is easy to misdiagnose

**Desktop readers show the file perfectly.** A ZIP's central directory lives at the end of the
file, so tolerant readers — Calibre, Python's `zipfile` — silently absorb the prefix. Only
readers that enforce `mimetype` at offset 0 reject it. The bug therefore presents as a device
*rendering* fault.

**The cover and table of contents still work while every page is blank.** Those are cached in
`KoboReader.sqlite` and `.kobo-images` when the book is indexed; page content is read live from
the file. A container damaged after indexing renders exactly this way.

**Opening a book re-downloads it.** A repair survives only until the Edition is read. Of seven
repaired Editions on 2026-09-20, the five never opened stayed clean and both that were opened
came back prefixed, with a device-written 1980 timestamp.

## The Editions are not implicated

Verified on 2026-09-20. Four variants built from a delivered Edition — the shipped markup
unchanged, `<main>` replaced by `<div>`, `<main>` unwrapped, and explicit `display: block` for
the HTML5 sectioning elements — were sideloaded over USB and **all four read normally**. Two
sideloaded default-namespace builds from 2026-09-06 had already been read to 25% and 16% on the
same device.

Across 25 files on the device, every payload matched Drive byte for byte, damaged or not.
Nothing is lost in transit; bytes are only ever added in front.

So blank pages mean a damaged container. They have never meant a markup fault, and PR #107's
default-namespace serialization is not implicated.

## It is intermittent, and the index is affected too

Individual downloads fail intermittently rather than always. The device has held **the same
Drive file downloaded twice** — one copy intact, one prefixed — which rules out the file, its
size, and its folder as the trigger.

Re-linking the Drive account measurably improves the success rate for a while. It does not
repair files already on disk, and the failure rate degrades again over subsequent days.

The same append-instead-of-replace pattern appears in the library index. Re-downloading an
already-indexed Edition appends a fresh set of chapter rows without clearing the old set:

```
2026-09-18-daily    18 chapter rows   <- 9 spine documents, listed twice
every other book     9 chapter rows
```

The stale set renders as empty chapters beside the real ones. Both faults are a missing reset
before a rewrite, in two different subsystems.

## `kobo-repair`

```
epub-news-feeder kobo-repair --volume /Volumes/KOBOeReader            # report only
epub-news-feeder kobo-repair --volume /Volumes/KOBOeReader --apply    # repair
```

It reports every Drive download that opens with an error body instead of its own signature, and
with `--apply` strips the prefix — but only after downloading what Drive holds under that name
and finding the digests identical, so it cannot write bytes that differ from the delivered
Edition. Drive's own `md5Checksum` is deliberately not trusted for this; only the bytes prove
it. Give `--drive-folder` twice, or set `GOOGLE_DRIVE_FOLDER_ID` and
`GOOGLE_DRIVE_FOLDER_ARCHIVE`, so an archived Edition is still found.

Originals are backed up under their source-relative path before anything is rewritten, the
replacement is written to a unique temporary and renamed over the original, and the result is
read back. A download with no recoverable payload behind the prefix is left for the device to
fetch again rather than truncated into a plausible-looking ruin.

Treat it as a diagnostic that also repairs, not a cure: opening an Edition re-downloads it.

## The scan log

Every run records what it found, whether or not anything was damaged — a clean scan is a data
point too. Records append to one file per month under `--log-dir` (`.local/kobo-scans` by
default, gitignored) and are copied to the Drive state folder named by `--log-folder`, which
defaults to `GOOGLE_DRIVE_FOLDER_DB`, so the record accumulates centrally rather than on one
machine. A failed upload is reported and never fails the scan; `--no-log` skips both.

```json
{"at": "2026-09-21T07:14:02Z", "firmware": "5.18.270971",
 "counts": {"downloads": 25, "damaged": 2, "repaired": 2, "uncertain": 0, "unchanged": 0},
 "damaged": [{"name": "2026-09-22-daily-….epub", "folder": "01_daily_news", "prefix_bytes": 507}],
 "outcomes": [{"name": "2026-09-22-daily-….epub", "status": "repaired", "reason": "stripped 507 bytes"}]}
```

The device serial is deliberately not recorded, though the firmware version is: these records
are meant to be shareable, including with the upstream report, and a serial identifies the
hardware rather than the fault.

This exists because **nobody has a measured failure rate for this bug, including Kobo**. The
evidence so far is a handful of dated observations. Enough of these records answers how often
downloads actually fail, and whether re-linking the account decays the way it appears to.

## Options that were investigated and rejected

| Option | Why not |
|---|---|
| Re-link Drive on the device | Helps for a while, repairs nothing already on disk, degrades again |
| `api_endpoint` retargeting to a self-hosted sync server | Redirects `storeapi.kobo.com`, the Kobo store protocol. It never touches the Drive download path. Firmware also restores `Kobo eReader.conf` on this device |
| Edge reverse proxy in front of Google's APIs | Requires DNS redirection plus a CA the device trusts, which requires rooting it. No published case of intercepting Nickel's TLS |
| `kobopatch` binary patch to libnickel | kobopatch does not support firmware 5.x at all, NickelMenu (its prerequisite) does not either, and no Libra Colour patches exist. Documented factory-reset risk |
| Migrating to Dropbox | Untested, and not the safe bet it looks. Dropbox tokens expire after 4 hours and it also returns a JSON body on 401, so the precondition exists there too; published patches show `supportsGoogleDrive` and `supportsDropbox` as sibling methods patched with identical bytes, suggesting one shared framework. Worth one experiment, not a migration |

USB sideloading has never failed in testing and remains the reliable path when an Edition has
to be readable now.
