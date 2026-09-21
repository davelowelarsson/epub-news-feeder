"""Repair of Google Drive downloads that a Kobo device damaged while writing them.

Kobo firmware 5.18 writes the body of a failed Drive request into the destination file, then
refreshes its access token, retries, and appends the real bytes instead of truncating first.
The result is an intact EPUB behind a Google ``401`` JSON error body. Nothing in the delivery
pipeline can prevent this — the file on Drive is already correct — so the repair happens on the
device, and only once Drive has vouched for the payload byte for byte.

The device is removable and the files are someone's books, so every write here is arranged so
that the failure modes leave the original in place: the bytes that were verified are the bytes
written, the replacement is atomic, and a backup never overwrites another backup.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

# A damaged download opens with a JSON error object rather than the format's own signature.
_ERROR_SENTINEL = b"{"
_SIGNATURES = (b"PK\x03\x04", b"%PDF-")

# Far beyond the 507-byte body observed in practice, yet small enough that a signature found
# later in the file is content rather than a prefix boundary.
_MAX_PREFIX_BYTES = 8192

# A Kobo firmware version, as `.kobo/version` writes it: dotted numbers and nothing else.
_FIRMWARE = re.compile(r"^\d+(?:\.\d+)+$")

# Files the Drive tree accumulates that the device never downloaded: AppleDouble sidecars and
# other dot-files macOS leaves on a FAT volume, and the fragments `fsck_msdos` salvages. Counting
# them would inflate the denominator of the damage rate these scans exist to establish, and a
# repair that leaves one behind inflates it permanently.
_FSCK_SALVAGE = re.compile(r"^FSCK\d+\.\d+$", re.IGNORECASE)


def _is_artefact(path: Path) -> bool:
    """Whether a file in the Drive tree is local debris rather than something Drive delivered."""

    return path.name.startswith(".") or bool(_FSCK_SALVAGE.match(path.name))


def _downloads(root: Path) -> Iterable[Path]:
    """Every file under the Drive tree that the device actually downloaded."""

    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink() and not _is_artefact(path):
            yield path


class KoboRepairError(Exception):
    """A Kobo volume that cannot be repaired."""


@dataclass(frozen=True, slots=True)
class DamagedDownload:
    """One on-device download carrying a recoverable payload behind an error body."""

    path: Path
    relative_path: Path
    prefix_bytes: int
    payload_sha256: str
    source_sha256: str

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True, slots=True)
class RepairOutcome:
    """What happened to one damaged download.

    ``uncertain`` separates the case where the device was modified but the result could not be
    confirmed from the case where nothing was touched. Reporting the two alike would send the
    reader looking in the wrong place; the backup is named so they can recover by hand.
    """

    name: str
    repaired: bool
    reason: str
    uncertain: bool = False
    backup: Path | None = None
    relative_path: Path | None = None

    @property
    def folder(self) -> str:
        return str(self.relative_path.parent) if self.relative_path else ""


def drive_download_root(volume: Path) -> Path:
    """Locate the Drive download tree on a mounted Kobo, refusing anything else."""

    root = volume / ".kobo" / "google_drive"
    if not root.is_dir():
        raise KoboRepairError(f"{volume} is not a mounted Kobo with Google Drive downloads")
    return root


def damaged_downloads(root: Path) -> tuple[DamagedDownload, ...]:
    """Find downloads whose payload is intact but preceded by an error body.

    A file is only reported when a format signature is actually present behind the prefix, so a
    download that failed outright is left for the device to fetch again rather than "repaired"
    into a truncated file. Symbolic links are ignored: a repair resolves them and would write
    through to a file outside the Drive tree. Local debris is ignored for the same reason the
    count ignores it — it was never downloaded, so it cannot be a damaged download.
    """

    damaged: list[DamagedDownload] = []
    for path in _downloads(root):
        with path.open("rb") as stream:
            header = stream.read(_MAX_PREFIX_BYTES)
        if header.startswith(_SIGNATURES) or not header.startswith(_ERROR_SENTINEL):
            continue
        offset = _payload_offset(header)
        if offset is None:
            continue
        # Only a download that is actually damaged is worth pulling into memory whole.
        body = path.read_bytes()
        damaged.append(
            DamagedDownload(
                path=path,
                relative_path=path.relative_to(root),
                prefix_bytes=offset,
                payload_sha256=sha256(body[offset:]).hexdigest(),
                source_sha256=sha256(body).hexdigest(),
            )
        )
    return tuple(damaged)


def repair_downloads(
    damaged: Iterable[DamagedDownload],
    *,
    drive_digests: Callable[[str], Sequence[str]],
    backup_directory: Path,
) -> tuple[RepairOutcome, ...]:
    """Strip the error body from each download Drive vouches for, backing the original up first.

    ``drive_digests`` answers with the digest of every file Drive holds under that name, across
    however many folders were configured, or an empty sequence when Drive holds none. A download
    is rewritten only when the payload read at write time matches one of them, so the bytes that
    were verified are exactly the bytes written.
    """

    outcomes: list[RepairOutcome] = []
    for download in damaged:
        outcomes.append(_repair_one(download, drive_digests, backup_directory))
    return tuple(outcomes)


def _repair_one(
    download: DamagedDownload,
    drive_digests: Callable[[str], Sequence[str]],
    backup_directory: Path,
) -> RepairOutcome:
    return replace(
        _repair_outcome(download, drive_digests, backup_directory),
        relative_path=download.relative_path,
    )


def _repair_outcome(
    download: DamagedDownload,
    drive_digests: Callable[[str], Sequence[str]],
    backup_directory: Path,
) -> RepairOutcome:
    # Up to and including the backup, every failure leaves the device exactly as it was.
    try:
        body = download.path.read_bytes()
        if sha256(body).hexdigest() != download.source_sha256:
            return RepairOutcome(download.name, False, "changed since it was scanned")
        payload = body[download.prefix_bytes :]
        digest = sha256(payload).hexdigest()
        known = tuple(drive_digests(download.name))
        if not known:
            return RepairOutcome(download.name, False, "not on Drive under that name")
        if digest not in known:
            return RepairOutcome(download.name, False, "payload does not match any file on Drive")
        backup = _backup(download, body, backup_directory)
        # The Drive lookup is a network round trip, so re-read rather than trust the snapshot
        # taken before it. This narrows the window to the rename below; it cannot close it,
        # because check-and-rename is not atomic on any filesystem we run on.
        if sha256(download.path.read_bytes()).hexdigest() != download.source_sha256:
            return RepairOutcome(download.name, False, "changed while it was being repaired")
        _replace_atomically(download.path, payload)
    except Exception as error:
        return RepairOutcome(download.name, False, f"left untouched: {error}")

    # Past the rename the device has changed, so a failure here is uncertainty, not inaction.
    try:
        landed = sha256(download.path.read_bytes()).hexdigest()
    except Exception as error:
        return RepairOutcome(
            download.name, False, f"replaced but could not be read back: {error}", True, backup
        )
    if landed != digest:
        return RepairOutcome(
            download.name,
            False,
            "what landed on the device does not match what was sent",
            True,
            backup,
        )
    return RepairOutcome(download.name, True, f"stripped {download.prefix_bytes} bytes")


def _backup(download: DamagedDownload, body: bytes, backup_directory: Path) -> Path:
    """Preserve the original under its source-relative path, never overwriting another backup."""

    target = backup_directory / download.relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    attempt, candidate = 0, target
    while True:
        try:
            descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            attempt += 1
            candidate = target.with_name(f"{target.name}.{attempt}")
            continue
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directory(candidate.parent)
        return candidate


def _replace_atomically(path: Path, payload: bytes) -> None:
    """Write the payload beside the original, then swap it in with a single rename.

    A truncating write to the device would leave a destroyed book behind a disconnected cable.
    The temporary name is unique per run so that two repairs of the same Edition cannot truncate
    one another's half-written replacement. Note the limit of the guarantee: ``os.replace`` is
    atomic against other processes, but FAT keeps no journal, so a power loss mid-rename can
    still leave the directory inconsistent. The backup is the answer to that, not this function.
    """

    sidecar = path.with_name(f"._{path.name}")
    inherited = sidecar.exists()
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.repair-")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        # Cleanup must never displace the exception that brought us here — losing a
        # KeyboardInterrupt to an unlink error would make the outcome a lie.
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise
    if not inherited:
        # The msdos volume materialises an AppleDouble beside a file renamed into place. It is
        # ours, it is counted as a download for the life of the device, and nothing reads it —
        # but one that predates this run is someone else's and stays.
        with suppress(OSError):
            sidecar.unlink(missing_ok=True)
    _sync_directory(path.parent)


def _sync_directory(directory: Path) -> None:
    """Best-effort durability for the directory entry itself.

    Filesystems differ on whether a directory can be opened and synced at all, and msdos
    volumes generally cannot, so this narrows the window where it is supported and is silent
    where it is not rather than failing a repair that otherwise succeeded.
    """

    with suppress(OSError):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _payload_offset(body: bytes) -> int | None:
    offsets = [
        offset
        for offset in (body.find(signature, 1, _MAX_PREFIX_BYTES) for signature in _SIGNATURES)
        if offset > 0
    ]
    return min(offsets) if offsets else None


def device_firmware(volume: Path) -> str | None:
    """Read the firmware version from a mounted Kobo, and nothing else from that file.

    ``.kobo/version`` is one comma-separated line that *leads with the device serial*. Only a
    field that actually looks like a firmware version is returned, and only from the first line:
    these records are meant to be shared, including with the upstream bug report, and the field
    beside the version is an identifier for someone's hardware. Anything unexpected is reported
    as unknown rather than guessed at.
    """

    path = volume / ".kobo" / "version"
    if path.is_symlink():
        return None
    try:
        first = path.read_text(encoding="utf-8", errors="strict").splitlines()[0]
    except (OSError, IndexError, UnicodeDecodeError):
        return None
    fields = first.split(",")
    if len(fields) < 3:
        return None
    candidate = fields[2].strip()
    return candidate if _FIRMWARE.match(candidate) else None


def download_count(root: Path) -> int:
    """How many Drive downloads exist on the device, damaged or not."""

    return sum(1 for _ in _downloads(root))


def scan_record(
    *,
    volume: Path,
    damaged: Sequence[DamagedDownload],
    outcomes: Sequence[RepairOutcome],
    at: datetime,
) -> dict[str, Any]:
    """Summarise one scan of a device, in terms that stay meaningful months later.

    The corruption is intermittent and nobody — including Kobo — has a measured failure rate for
    it. One of these per visit accumulates into that measurement, which is why the record keeps
    counts and filenames rather than only what was repaired.
    """

    def status(outcome: RepairOutcome) -> str:
        if outcome.repaired:
            return "repaired"
        return "uncertain" if outcome.uncertain else "unchanged"

    statuses = [status(outcome) for outcome in outcomes]
    return {
        "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "firmware": device_firmware(volume),
        "counts": {
            "downloads": download_count(volume / ".kobo" / "google_drive"),
            "damaged": len(damaged),
            "repaired": statuses.count("repaired"),
            "uncertain": statuses.count("uncertain"),
            "unchanged": statuses.count("unchanged"),
        },
        "damaged": [
            {
                "name": download.name,
                "folder": str(download.relative_path.parent),
                "prefix_bytes": download.prefix_bytes,
            }
            for download in damaged
        ],
        "outcomes": [
            {"name": o.name, "folder": o.folder, "status": s, "reason": o.reason}
            for o, s in zip(outcomes, statuses, strict=True)
        ],
    }


def append_scan(record: Mapping[str, Any], *, log_directory: Path) -> Path:
    """Append one scan to the log for its month, and answer which file that was.

    One file per month keeps the record appendable, small enough to upload whole after every
    scan, and bounded in number.
    """

    log_directory.mkdir(parents=True, exist_ok=True)
    path = log_directory / f"kobo-scans-{str(record['at'])[:7]}.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
    return path
