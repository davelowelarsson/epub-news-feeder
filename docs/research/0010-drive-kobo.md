# Research 0010: Google Drive delivery and Kobo handoff

Status: recommendation for [issue #10](https://github.com/davelowelarsson/epub-news-feeder/issues/10)

## Decision summary

For the first remote delivery, authenticate to **the personal Google account
linked to the reader's Kobo** with a narrowly scoped user OAuth grant, then
create each Edition directly in that account's automatically-created `Rakuten
Kobo` folder as a non-DRM EPUB. Kobo then downloads it when the reader syncs
over Wi-Fi. This is the smallest reliable remote path; it requires no Kobo API,
Calibre installation, USB connection, Google shared drive, or long-lived
service-account key.

Use a one-time interactive OAuth setup to select/grant the destination folder,
and retain the resulting refresh token as a production secret. At run time use
`drive.file`, never the broad `drive` scope unless a future setup flow proves it
cannot operate with per-file access. Generate a Drive file ID before upload and
persist the Run ID-to-file-ID transaction privately so retries converge.

**Do not choose Drive as the authoritative state store in this issue.** Drive
may hold Editions, but Article/Edition state, transaction recovery, and
retention policy remain decisions for issue #5; configuration field names and
which values are secret remain decisions for issue #4. The privileged scheduled
workflow and secret/OIDC boundary are constrained by issue #9.

## Dependencies and decisions still open

| Dependency | Why #10 cannot settle it | Required decision |
| --- | --- | --- |
| [#4 configuration](https://github.com/davelowelarsson/epub-news-feeder/issues/4) | Folder selection, credential-mode choice, edition naming inputs, and retention must be represented without committing private values. | Define a nonsecret delivery shape plus references to secrets/private configuration. |
| [#5 state](https://github.com/davelowelarsson/epub-news-feeder/issues/5) | A retry needs a durable Run ID, Drive file ID, checksum, and state version; source and deduplication state need stronger semantics than a delivery folder. | Select the authoritative private state backend and transaction/compare-and-swap model. |
| [#9 private scheduled operation](https://github.com/davelowelarsson/epub-news-feeder/issues/9) | OAuth refresh token and destination ID must never be exposed to PRs, logs, caches, artifacts, or commits. | Supply the default-branch-only production environment, secret handling, concurrency, and sanitized diagnostics recommended in report 0009. |
| Reader model and operator preference | Kobo's Drive feature is not universal, and Kobo does not document a machine-to-device API. | Confirm a supported Kobo model and whether automatic Wi-Fi pickup or manual Calibre/USB transfer is wanted. |

## Facts established from official documentation

### Google Drive identity and least privilege

- Google distinguishes user OAuth from service accounts: a service account acts
  as the application, while OAuth acts on a user's data after consent. OAuth
  access tokens are short-lived; a refresh token is the durable credential for
  unattended access and can stop working after revocation, inactivity, policy
  changes, or other events. [Google OAuth overview][1]
- Google recommends narrowly focused, non-sensitive Drive scopes where
  possible. `https://www.googleapis.com/auth/drive.file` can create Drive files
  and modify files opened/shared with the application; broad `drive` can manage
  every Drive file and is restricted. Refresh tokens require secure long-term
  storage. [Drive API scopes][2]
- A service account has no storage quota and cannot own files. Google says it
  must upload into a shared drive or use OAuth 2.0 on behalf of a human user.
  Shared-drive files are organization-owned and have a different membership
  model from My Drive. [Drive folders][3] [shared drives][4]
- GitHub Actions OIDC can exchange a GitHub OIDC token for short-lived Google
  Cloud credentials through Workload Identity Federation. Google requires an
  attribute condition for the shared GitHub issuer and recommends immutable
  numeric GitHub IDs over reclaimable names. Its example requires
  `id-token: write` and normally impersonates a service account. [Google Cloud
  Workload Identity Federation][5]

**Implication.** OIDC is technically feasible for a dedicated service account
that writes a Google **shared drive** or other Google Cloud state backend. It
is not the first Kobo delivery choice: Kobo requires a folder in the linked
Google account, whereas service accounts cannot own My Drive files. Do not
paper over that ownership mismatch by placing a service-account key in Actions.
Re-evaluate OIDC only if delivery separates from Kobo (for example, service
account to a shared-drive archive) or the operator selects a Workspace shared
drive and verifies Kobo can read its contents.

### Upload and retry facts

- Drive supports simple/multipart uploads for small files and resumable uploads
  for larger or interruption-prone transfers. A resumable session can be
  queried after interruption and expires after one week. [Drive uploads][6]
- Drive can generate file IDs before a `files.create` request. The upload guide
  states that retrying an upload with a pre-generated ID after an indeterminate
  failure avoids duplicates: a completed create returns `409 Conflict` on a
  subsequent retry. `drive.file` authorizes `files.generateIds`. [Drive
  uploads][6] [generateIds reference][7]
- Drive can search the requesting application's files by `appProperties`, which
  permits a private application-side Run ID marker. [Drive search][8]

### Kobo and Calibre handoff facts

- Kobo's Drive integration links a Kobo account to a Google account and creates
  a `Rakuten Kobo` folder. Kobo says files must be in that folder; a Wi-Fi sync
  downloads new books. It supports this feature only on Kobo Forma, Sage,
  Elipsa, Elipsa 2E, and Libra Colour with software 4.37 or later. It handles
  non-protected EPUB/PDF, not DRM-protected files. [Kobo Google Drive guide][9]
- Kobo lists EPUB, EPUB2, and EPUB3 as supported eReader formats. It does not
  list KEPUB as an upload format in that compatibility guide. [Kobo file
  formats][10]
- USB sideloading is a supported fallback: connect the reader, copy a
  non-protected EPUB to the `KOBOeReader` drive, eject it, and Kobo imports the
  content. Kobo notes such content appears only on the device where it was
  imported. [Kobo USB sideloading][11]
- Calibre documents direct support for major ebook readers and its
  “Connect to folder” route for readers exposed as USB disks. It is therefore a
  local, operator-mediated USB fallback, not a remote Kobo handoff protocol.
  [Calibre device FAQ][12]

**Kobo constraint.** “Drive sync” does not mean generic Drive or background
push to every Kobo. The Edition must land in the linked account's `Rakuten
Kobo` folder; the reader must be one of the supported models, online, and
synced. The supplied documentation gives no basis for using arbitrary Drive
folders, shared drives, an API to trigger sync, guaranteed immediate delivery,
or KEPUB as the remote wire format. Keep first delivery as standards-compliant
`.epub`; treat KEPUB conversion as an optional, later Calibre/device feature
that requires its own compatibility test.

## Recommended first-remote workflow

### One-time operator setup

1. Confirm the Kobo model is in Kobo's Drive-support list, update it to 4.37+
   and link the *same* personal Google account on the reader. This creates
   `Rakuten Kobo`.
2. Run a local, interactive OAuth setup owned by the operator. It must request
   only `drive.file`, use a folder-picker/explicit-grant flow to select the
   `Rakuten Kobo` folder, and record its opaque folder ID. This setup capability
   is a **configuration dependency on #4**; do not assume a headless scheduled
   runner can discover arbitrary pre-existing Drive folders under `drive.file`.
3. Store the resulting refresh token and private folder ID as separate secrets
   in the production environment designed by #9. Store neither in tracked YAML,
   Actions variables, command arguments, logs, artifacts, caches, or a commit.
   Store client credentials separately if the client type requires them.
4. Test the exact account/folder/device combination by uploading a harmless
   test EPUB, syncing Kobo over Wi-Fi, opening it, and checking the expected
   title/cover. This validates a vendor-controlled integration before scheduled
   publication is enabled.

### Each scheduled publication

1. #9's default-branch-only production job validates the secret **names** and
   required configuration without printing values, then obtains an access token
   using the stored refresh token.
2. Generate a valid EPUB in the runner's private temporary directory. Validate
   it locally. Do not generate KEPUB for this path.
3. Ask Drive for one pre-generated file ID, then write a private pending
   transaction `{Run ID, Drive file ID, filename, byte count, content digest}`
   to the state backend selected by #5.
4. Create that exact file ID in the selected `Rakuten Kobo` folder with
   `mimeType: application/epub+zip`, `appProperties.run_id`, and a stable,
   non-content-bearing filename. Use a resumable upload; retain the upload URI
   only in private transient/recovery state.
5. On a network ambiguity, query the resumable session; if necessary retry the
   same pre-generated ID. A `409` is a reconciliation signal, not an instruction
   to make another Edition. Also look up the private Run ID property and verify
   ID, byte count, and digest before declaring success.
6. Mark the private transaction published, commit the corresponding Article and
   Edition state with the #5 concurrency rule, then emit only the approved
   sanitized status and Run ID. The reader performs its own later Wi-Fi sync.

This is deliberately *at-least-once transport with exactly-one visible Edition
per Run ID*, not exactly-once end-to-end reading. Kobo's sync/download is an
external, asynchronous handoff and cannot be transactionally coupled to the
publication run.

## Folder layout, file naming, and retention

For the initial Kobo integration, put Editions as direct children of
`Rakuten Kobo`; Kobo explicitly requires that folder and does not promise
recursive discovery. Do not create a private nested directory until an actual
reader test demonstrates it appears in My Books.

Use an immutable filename that sorts chronologically without exposing article
titles, section labels, interests, or source URLs, for example:

    epub-news--2026-08-08T060000Z--<run-id>.epub

The timestamp is the Publication time defined by the still-pending configuration
and Run ID design, not runner-local time. Never overwrite “latest.epub”: Kobo
may have downloaded the prior file and replacement offers weak recovery and
reader-history semantics.

Start with **no automatic deletion**. Retention is a product/state decision:
deleting a Drive file does not establish that Kobo removed its locally imported
copy, and Drive cannot know what the reader downloaded. Once #4 and #5 specify
an Edition-history policy, implement a conservative reconciled retention job
(for example, keep a configured count of successfully published Editions), but
never delete the only copy before the authoritative state records the Edition
and no in-flight retry references its Drive ID. Manual deletion from Drive is
the safe first operational procedure.

## State versus output boundary

| Data | Location in first remote design | Authority | Notes |
| --- | --- | --- | --- |
| EPUB Edition | Private `Rakuten Kobo` folder | Delivery copy | Reader-visible, immutable per Run ID; not durable process state. |
| Run ID → Drive ID, digest, phase | Private state backend (#5) | Recovery authority | Needed to distinguish retry from a new Edition. |
| Article identity, selection, source health, Edition history | Private state backend (#5) | System authority | Must not be inferred solely from mutable Drive contents. |
| Refresh token, client values, folder ID | Production secrets (#9) | Credentials/configuration | Revocable configuration, never mutable state. |
| Resumable session URI | Runner memory or private pending transaction | Short-lived recovery aid | Expires after a week; never log it. |
| GitHub Actions logs/artifacts/cache/commit | None of the above | Never authority | Issue #9 excludes private output/state from these public-repository surfaces. |

Drive's app-data space is an OAuth-accessible private application-data option,
but selecting it as state would still require #5 to define durability, backup,
concurrency, inspection, recovery, and retention. This report does not make
that selection merely because it is adjacent to delivery.

## Threat model and controls

| Threat | Consequence | Control |
| --- | --- | --- |
| PR/fork or public log obtains OAuth material | Attacker reads/writes the personal Drive and Editions | Apply #9's isolated production environment; no secrets or OIDC in PR jobs; safe allowlisted logs. |
| Overbroad OAuth grant | Publisher can read/delete unrelated Drive files | Default to `drive.file`, explicit folder grant, one dedicated Google account if desired; do not use restricted `drive` as convenience. |
| Refresh-token loss or revocation | Persistent unauthorized access or failed publishing | Store as a separate environment secret, rotate/revoke in Google, fail closed and alert with a value-free diagnostic. |
| Service-account key added to Actions | Long-lived credential compromise and ownership failure | No service-account key for Kobo delivery; use user OAuth. OIDC only for a separately justified shared-drive/cloud backend. |
| Timeout/retry creates two files | Duplicate reader Editions | Pre-generate and persist one file ID per Run ID; resumable recovery, 409 reconciliation, property/digest verification, #9 concurrency. |
| Crash between Drive upload and state commit | Re-run cannot distinguish published/unpublished | Persist pending transaction before upload; reconcile Drive by file ID/Run ID before creating anything. |
| Filename/log exposes private interests or content | Cloud/Actions disclosure | Neutral name, no titles/URLs/section labels; destination ID and API responses stay private. |
| Kobo integration changes or reader is unsupported/offline | Edition does not reach reader | Startup device acceptance test; treat Drive publish as delivery success and Kobo sync as an external handoff; retain USB/Calibre fallback. |
| Retention deletes a still-needed edition | Lost archive or inconsistent reader library | No automatic deletion initially; later delete only under #4/#5's reconciled state policy. |

## Alternatives

| Option | Assessment |
| --- | --- |
| **User OAuth to linked account's `Rakuten Kobo` folder** | **Recommended.** Minimal remote path; compatible with Kobo's documented feature. Cost: long-lived refresh token and one-time interactive consent. |
| Service account + GitHub OIDC to shared drive | Good keyless architecture for a non-Kobo archive/state store, but does not solve the documented My Drive `Rakuten Kobo` requirement; needs Workspace/shared-drive ownership decisions and a Kobo compatibility experiment. |
| Service-account JSON key to personal Drive folder | Reject. Long-lived key; service accounts cannot own files; weaker than OAuth/OIDC and does not establish Kobo compatibility. |
| Broad user `drive` OAuth | Last-resort setup compatibility fallback only, after proving `drive.file` cannot select/create in the Kobo folder. It is restricted and grants full Drive management, so it needs explicit operator approval and a written revocation/rotation plan. |
| Calibre USB / direct file copy | Reliable local fallback and useful for unsupported Kobos, but not remote, unattended delivery. It asks the operator to connect the reader. |
| KEPUB conversion | Defer. Kobo documents EPUB support for the Drive route, not KEPUB; conversion/device behavior must be tested separately. |

## Acceptance checks for implementation

- A controlled setup demonstrates that a `drive.file` OAuth grant can create a
  non-DRM EPUB in the linked account's `Rakuten Kobo` folder without requesting
  broad `drive`; if it cannot, the implementation stops and records the
  explicit scope decision rather than silently escalating.
- On a supported Kobo with 4.37+, Wi-Fi sync makes that exact test EPUB
  available and readable.
- Source/tests show user OAuth for the Kobo path and no service-account JSON
  key, broad Drive scope, or Google Cloud OIDC configuration is required for
  that path.
- A forced interrupted upload resumes or retries the same Drive file ID and
  leaves exactly one file with the Run ID property.
- A simulated crash after upload but before state commit reconciles the existing
  file without producing another Edition.
- Two overlapping production dispatches serialize under #9 and preserve the
  #5 state version rule.
- Production logs, job summaries, artifacts, caches, and commits contain no
  EPUB, refresh token, folder ID, upload URI, filename if considered private,
  article/section/source data, or Drive API response.
- USB/Calibre fallback is documented for an unsupported reader, and no claim is
  made that it is remote or that KEPUB is required.

## Official sources

[1]: https://developers.google.com/identity/protocols/oauth2
[2]: https://developers.google.com/workspace/drive/api/guides/api-specific-auth
[3]: https://developers.google.com/workspace/drive/api/guides/folder
[4]: https://developers.google.com/workspace/drive/api/guides/about-shareddrives
[5]: https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines
[6]: https://developers.google.com/workspace/drive/api/guides/manage-uploads
[7]: https://developers.google.com/workspace/drive/api/reference/rest/v3/files/generateIds
[8]: https://developers.google.com/workspace/drive/api/guides/search-files
[9]: https://help.kobo.com/hc/en-us/articles/15335985512983-Add-books-to-your-eReader-using-Google-Drive
[10]: https://help.kobo.com/hc/en-us/articles/360017763713-File-formats-your-Kobo-eReader-and-Kobo-Books-app-support
[11]: https://help.kobo.com/hc/en-us/articles/360024775093-Add-non-protected-PDF-and-ePub-files-to-your-Kobo-eReader-using-your-computer
[12]: https://manual.calibre-ebook.com/faq.html
