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

import os
from collections.abc import Callable, Iterable, Sequence
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
    relative_path: Path
    prefix_bytes: int
    payload_sha256: str
    source_sha256: str

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
    into a truncated file. Symbolic links are ignored: a repair resolves them and would write
    through to a file outside the Drive tree.
    """

    damaged: list[DamagedDownload] = []
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        if path.is_symlink():
            continue
        body = path.read_bytes()
        if body.startswith(_SIGNATURES) or not body.startswith(_ERROR_SENTINEL):
            continue
        offset = _payload_offset(body)
        if offset is None:
            continue
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
        _backup(download, body, backup_directory)
        _replace_atomically(download.path, payload)
        if sha256(download.path.read_bytes()).hexdigest() != digest:
            return RepairOutcome(
                download.name, False, "what landed on the device does not match what was sent"
            )
    # Any failure here — Drive, filesystem, a yanked cable — must report what happened to
    # this download rather than abandon the ones already repaired without a word.
    except Exception as error:
        return RepairOutcome(download.name, False, f"left untouched: {error}")
    return RepairOutcome(download.name, True, f"stripped {download.prefix_bytes} bytes")


def _backup(download: DamagedDownload, body: bytes, backup_directory: Path) -> None:
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
        return


def _replace_atomically(path: Path, payload: bytes) -> None:
    """Write the payload beside the original, then swap it in with a single rename.

    A truncating write to the device would leave a destroyed book behind a disconnected cable.
    """

    temporary = path.with_name(f".{path.name}.repair-tmp")
    try:
        with open(temporary, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _payload_offset(body: bytes) -> int | None:
    offsets = [
        offset
        for offset in (body.find(signature, 1, _MAX_PREFIX_BYTES) for signature in _SIGNATURES)
        if offset > 0
    ]
    return min(offsets) if offsets else None
