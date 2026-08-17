"""Scheduled State Store persistence through the Drive Delivery Target boundary.

A GitHub-hosted runner starts with an empty filesystem: without this, every scheduled run
begins with an empty State Store and re-delivers everything it has already delivered. The
SQLite State Store and its ``.key`` fingerprint sidecar move together as one archived unit so
they can never diverge — losing the key does not lose data, but it loses comparability of
revision fingerprints, so every Article looks new again.

Restore is fail-closed: an archive that is present but cannot be verified aborts the run
rather than silently proceeding with an empty store. Save happens only after a successful,
finalized Edition delivery, mirroring the local State Store's own writer-lock discipline.

Drive is a private Delivery Target boundary reused as-is (``DriveClient``, ``HttpDriveClient``):
this module never talks to Drive directly and never widens its ``drive.file`` scope.
"""

from __future__ import annotations

import io
import os
import sqlite3
import tarfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from epub_news_feeder.diagnostics import Diagnostics
from epub_news_feeder.drive import DriveAuthError, DriveClient, DriveError

_ARCHIVE_CONTENT_TYPE = "application/gzip"
_DATABASE_ARCNAME = "state.sqlite3"
_KEY_ARCNAME = "state.sqlite3.key"

# What an operator does about it, in the one place the operator will read: the failure message.
_RENEW_INSTRUCTION = (
    "Google rejected the Drive refresh token; renew it with `epub-news-feeder authorize-drive`"
)


class StateSyncError(Exception):
    """Safe State Sync failure; never carries a credential value or fingerprint key bytes."""


class StateSyncAuthError(StateSyncError):
    """The credential itself was rejected, rather than the archive being unverifiable.

    A subclass, so every existing fail-closed ``except StateSyncError`` keeps behaving exactly
    as it did; callers that want to say something more useful can catch this first.
    """


@dataclass(frozen=True, slots=True)
class StateRestoreOutcome:
    """Whether an existing, verified archive was restored, or the run is a legitimate first run."""

    restored: bool


def state_archive_filename(environment: str) -> str:
    """A fixed, overwritten-in-place object name; state is mutable, unlike an Edition."""

    return f"state-{environment}.tar.gz"


def pack_state_archive(state_path: Path) -> bytes:
    """Pack the SQLite State Store and its ``.key`` sidecar into one archive, as one unit."""

    key_path = state_path.with_suffix(f"{state_path.suffix}.key")
    if not state_path.is_file() or not key_path.is_file():
        raise StateSyncError("State Store or its fingerprint key sidecar is missing")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(state_path, arcname=_DATABASE_ARCNAME)
        archive.add(key_path, arcname=_KEY_ARCNAME)
    return buffer.getvalue()


def unpack_state_archive(archive_bytes: bytes, *, state_path: Path) -> None:
    """Extract a packed archive to *state_path* and its ``.key`` sidecar, securely.

    Only the two expected, plain-file members are accepted; anything else (an unexpected
    name, a symlink, a directory) aborts rather than extracting.
    """

    key_path = state_path.with_suffix(f"{state_path.suffix}.key")
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
            members = {member.name: member for member in archive.getmembers()}
            if set(members) != {_DATABASE_ARCNAME, _KEY_ARCNAME}:
                raise StateSyncError("State archive did not contain exactly the expected members")
            for member in members.values():
                if not member.isfile():
                    raise StateSyncError("State archive member is not a plain file")
            state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            state_path.parent.chmod(0o700)
            _extract_member(archive, members[_DATABASE_ARCNAME], state_path)
            _extract_member(archive, members[_KEY_ARCNAME], key_path)
    except tarfile.TarError as error:
        raise StateSyncError("State archive could not be read") from error
    os.chmod(state_path, 0o600)
    os.chmod(key_path, 0o600)


def _extract_member(archive: tarfile.TarFile, member: tarfile.TarInfo, destination: Path) -> None:
    extracted = archive.extractfile(member)
    if extracted is None:
        raise StateSyncError("State archive member could not be read")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(extracted.read())


def restore_state(
    *,
    client: DriveClient,
    folder_id: str,
    state_path: Path,
    environment: str,
    diagnostics: Diagnostics | None = None,
) -> StateRestoreOutcome:
    """Restore the State Store from Drive, failing closed on anything but a verified copy.

    - Present and verifies: restore and report ``restored=True``.
    - Genuinely absent (a clean empty listing): a legitimate first run; report ``restored=False``
      and emit a distinct diagnostic so it is visible rather than silent.
    - Present but download fails, or its digest does not match: abort — never proceed empty.
    - Ambiguous existence (auth failure, transport error): abort, same as a digest mismatch.
    """

    filename = state_archive_filename(environment)
    try:
        existing = client.find_file(folder_id=folder_id, filename=filename)
    except DriveAuthError as error:
        raise StateSyncAuthError(_RENEW_INSTRUCTION) from error
    except DriveError as error:
        raise StateSyncError("Could not determine whether a State archive exists") from error
    if existing is None:
        if diagnostics is not None:
            diagnostics.emit("STATE_ABSENT", phase="state")
        return StateRestoreOutcome(restored=False)
    try:
        archive_bytes = client.download(file_id=existing.file_id)
    except DriveAuthError as error:
        raise StateSyncAuthError(_RENEW_INSTRUCTION) from error
    except DriveError as error:
        raise StateSyncError("State archive download failed") from error
    digest = sha256(archive_bytes).hexdigest()
    if existing.sha256 is None or existing.sha256 != digest:
        raise StateSyncError("State archive digest did not verify")
    unpack_state_archive(archive_bytes, state_path=state_path)
    if diagnostics is not None:
        diagnostics.emit("STATE_RESTORED", phase="state", digest=digest)
    return StateRestoreOutcome(restored=True)


def save_state(
    *,
    client: DriveClient,
    folder_id: str,
    state_path: Path,
    environment: str,
    connection: sqlite3.Connection | None = None,
    diagnostics: Diagnostics | None = None,
) -> str:
    """Checkpoint, pack, and upsert the State Store archive; overwritten in place each run.

    Unlike an Edition, state is mutable by design: an existing archive is updated in place
    (Drive keeps its own revision history as a safety net) rather than treated as immutable.
    """

    _checkpoint_wal(state_path, connection)
    filename = state_archive_filename(environment)
    archive_bytes = pack_state_archive(state_path)
    digest = sha256(archive_bytes).hexdigest()
    try:
        existing = client.find_file(folder_id=folder_id, filename=filename)
        if existing is not None:
            client.update(
                file_id=existing.file_id, content=archive_bytes, content_type=_ARCHIVE_CONTENT_TYPE
            )
        else:
            client.upload(
                folder_id=folder_id,
                filename=filename,
                content=archive_bytes,
                content_type=_ARCHIVE_CONTENT_TYPE,
            )
    except DriveAuthError as error:
        raise StateSyncAuthError(_RENEW_INSTRUCTION) from error
    except DriveError as error:
        raise StateSyncError("State archive save failed") from error
    if diagnostics is not None:
        diagnostics.emit("STATE_SAVED", phase="state", digest=digest)
    return digest


def _checkpoint_wal(state_path: Path, connection: sqlite3.Connection | None) -> None:
    """Merge the WAL into the main database file so the archived copy is self-contained."""

    if connection is not None:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return
    own_connection = sqlite3.connect(state_path, timeout=5, isolation_level=None)
    try:
        own_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        own_connection.close()
