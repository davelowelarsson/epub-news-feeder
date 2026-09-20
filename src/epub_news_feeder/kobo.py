"""Repair of Google Drive downloads that a Kobo device damaged while writing them.

Kobo firmware 5.18 writes the body of a failed Drive request into the destination file, then
refreshes its access token, retries, and appends the real bytes instead of truncating first.
The result is an intact EPUB behind a Google ``401`` JSON error body. Nothing in the delivery
pipeline can prevent this — the file on Drive is already correct — so the repair happens on the
device, and only once Drive has vouched for the payload byte for byte.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

# A damaged download opens with a JSON error object rather than the format's own signature.
_ERROR_SENTINEL = b"{"
_SIGNATURES = (b"PK\x03\x04", b"%PDF-")

# Far beyond the 507-byte body observed in practice, yet small enough that a signature found
# later in the file is content rather than a prefix boundary.
_MAX_PREFIX_BYTES = 8192


class KoboRepairError(Exception):
    """A Kobo volume that cannot be repaired."""


@dataclass(frozen=True, slots=True)
class DamagedDownload:
    """One on-device download carrying a recoverable payload behind an error body."""

    path: Path
    prefix_bytes: int
    payload_sha256: str

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True, slots=True)
class RepairOutcome:
    """What happened to one damaged download."""

    name: str
    repaired: bool
    reason: str


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
    into a truncated file.
    """

    damaged: list[DamagedDownload] = []
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        body = path.read_bytes()
        if body.startswith(_SIGNATURES) or not body.startswith(_ERROR_SENTINEL):
            continue
        offset = _payload_offset(body)
        if offset is None:
            continue
        payload = body[offset:]
        damaged.append(
            DamagedDownload(
                path=path,
                prefix_bytes=offset,
                payload_sha256=sha256(payload).hexdigest(),
            )
        )
    return tuple(damaged)


def repair_downloads(
    damaged: Iterable[DamagedDownload],
    *,
    expected_sha256: Callable[[str], str | None],
    backup_directory: Path,
) -> tuple[RepairOutcome, ...]:
    """Strip the error body from each download Drive vouches for, backing the original up first.

    ``expected_sha256`` answers with the digest of the file Drive holds under that name, or
    ``None`` when Drive no longer holds it. A download is rewritten only when its payload digest
    matches, which makes it impossible to write bytes that differ from the delivered Edition.
    """

    outcomes: list[RepairOutcome] = []
    for download in damaged:
        expected = expected_sha256(download.name)
        if expected is None:
            outcomes.append(RepairOutcome(download.name, False, "not on Drive under that name"))
            continue
        if expected != download.payload_sha256:
            outcomes.append(
                RepairOutcome(download.name, False, "payload does not match the file on Drive")
            )
            continue
        backup_directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(download.path, backup_directory / download.name)
        payload = download.path.read_bytes()[download.prefix_bytes :]
        download.path.write_bytes(payload)
        outcomes.append(
            RepairOutcome(download.name, True, f"stripped {download.prefix_bytes} bytes")
        )
    return tuple(outcomes)


def _payload_offset(body: bytes) -> int | None:
    offsets = [
        offset
        for offset in (body.find(signature, 1, _MAX_PREFIX_BYTES) for signature in _SIGNATURES)
        if offset > 0
    ]
    return min(offsets) if offsets else None
