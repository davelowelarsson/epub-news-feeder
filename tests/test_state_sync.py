from __future__ import annotations

import io
import sqlite3
import tarfile
from hashlib import sha256
from pathlib import Path

import pytest

from epub_news_feeder.drive import DriveError, DriveFile
from epub_news_feeder.state_sync import (
    StateSyncError,
    pack_state_archive,
    restore_state,
    save_state,
    state_archive_filename,
    unpack_state_archive,
)


class FakeDriveClient:
    """In-memory DriveClient double: no network, mirrors the Drive contract used by state_sync."""

    def __init__(self, *, find_file_error: bool = False) -> None:
        self.files: dict[str, tuple[str, bytes]] = {}
        self._next_id = 0
        self.upload_calls = 0
        self.update_calls = 0
        self.download_calls = 0
        self._find_file_error = find_file_error

    def find_file(self, *, folder_id: str, filename: str) -> DriveFile | None:
        del folder_id
        if self._find_file_error:
            raise DriveError("Drive file lookup failed")
        entry = self.files.get(filename)
        if entry is None:
            return None
        file_id, content = entry
        return DriveFile(file_id=file_id, sha256=sha256(content).hexdigest())

    def upload(
        self,
        *,
        folder_id: str,
        filename: str,
        content: bytes,
        content_type: str = "application/epub+zip",
    ) -> str:
        del folder_id, content_type
        self.upload_calls += 1
        self._next_id += 1
        file_id = f"drive-file-{self._next_id}"
        self.files[filename] = (file_id, content)
        return file_id

    def update(self, *, file_id: str, content: bytes, content_type: str) -> str:
        del content_type
        self.update_calls += 1
        for filename, (existing_id, _content) in self.files.items():
            if existing_id == file_id:
                self.files[filename] = (existing_id, content)
                return existing_id
        raise KeyError(file_id)

    def download(self, *, file_id: str) -> bytes:
        self.download_calls += 1
        for _filename, (existing_id, content) in self.files.items():
            if existing_id == file_id:
                return content
        raise KeyError(file_id)


class _FailingDownloadClient(FakeDriveClient):
    def download(self, *, file_id: str) -> bytes:
        raise DriveError("Drive download failed")


class _FailingUploadClient(FakeDriveClient):
    def upload(
        self,
        *,
        folder_id: str,
        filename: str,
        content: bytes,
        content_type: str = "application/epub+zip",
    ) -> str:
        raise DriveError("Drive upload failed")

    def update(self, *, file_id: str, content: bytes, content_type: str) -> str:
        raise DriveError("Drive update failed")


def _make_state_files(tmp_path: Path, *, db_content: bytes = b"sqlite-bytes") -> Path:
    state_path = tmp_path / "state.sqlite3"
    state_path.write_bytes(db_content)
    key_path = state_path.with_suffix(".sqlite3.key")
    key_path.write_bytes(b"\x00" * 32)
    key_path.chmod(0o600)
    return state_path


def _make_real_state_files(tmp_path: Path, *, table_value: str = "sample-value") -> Path:
    """A genuine, checkpointable SQLite State Store, for tests that exercise ``save_state``."""

    state_path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(state_path, isolation_level=None)
    try:
        connection.execute("CREATE TABLE t(value TEXT)")
        connection.execute("INSERT INTO t VALUES (?)", (table_value,))
    finally:
        connection.close()
    key_path = state_path.with_suffix(".sqlite3.key")
    key_path.write_bytes(b"\x00" * 32)
    key_path.chmod(0o600)
    return state_path


def _read_table_value(state_path: Path) -> str:
    connection = sqlite3.connect(state_path)
    try:
        row = connection.execute("SELECT value FROM t").fetchone()
    finally:
        connection.close()
    assert row is not None
    return str(row[0])


# --- pack / unpack -----------------------------------------------------------------


def test_state_sync_pack_and_unpack_round_trips_database_and_key_sidecar(tmp_path: Path) -> None:
    state_path = _make_state_files(tmp_path, db_content=b"the-database-bytes")
    key_path = state_path.with_suffix(".sqlite3.key")
    original_key = key_path.read_bytes()

    archive = pack_state_archive(state_path)

    restored_path = tmp_path / "restored" / "state.sqlite3"
    unpack_state_archive(archive, state_path=restored_path)

    assert restored_path.read_bytes() == b"the-database-bytes"
    restored_key_path = restored_path.with_suffix(".sqlite3.key")
    assert restored_key_path.read_bytes() == original_key


def test_state_sync_unpack_restores_restrictive_sidecar_permissions(tmp_path: Path) -> None:
    state_path = _make_state_files(tmp_path)
    archive = pack_state_archive(state_path)

    restored_path = tmp_path / "restored" / "state.sqlite3"
    unpack_state_archive(archive, state_path=restored_path)

    restored_key_path = restored_path.with_suffix(".sqlite3.key")
    assert oct(restored_path.stat().st_mode)[-3:] == "600"
    assert oct(restored_key_path.stat().st_mode)[-3:] == "600"


def test_state_sync_pack_requires_both_database_and_key_sidecar(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite3"
    state_path.write_bytes(b"only the database, no key sidecar")

    with pytest.raises(StateSyncError, match="fingerprint key sidecar is missing"):
        pack_state_archive(state_path)


def test_state_sync_unpack_rejects_archive_with_unexpected_members(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(name="unexpected.txt")
        payload = b"not the expected member"
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    with pytest.raises(StateSyncError, match="expected members"):
        unpack_state_archive(buffer.getvalue(), state_path=tmp_path / "state.sqlite3")


def test_state_sync_unpack_rejects_a_symlink_member(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        db_info = tarfile.TarInfo(name="state.sqlite3")
        db_info.size = 0
        archive.addfile(db_info, io.BytesIO(b""))
        key_info = tarfile.TarInfo(name="state.sqlite3.key")
        key_info.type = tarfile.SYMTYPE
        key_info.linkname = "/etc/passwd"
        archive.addfile(key_info)

    with pytest.raises(StateSyncError, match="plain file"):
        unpack_state_archive(buffer.getvalue(), state_path=tmp_path / "state.sqlite3")


def test_state_sync_unpack_rejects_corrupt_archive_bytes(tmp_path: Path) -> None:
    with pytest.raises(StateSyncError, match="could not be read"):
        unpack_state_archive(b"not a tarball at all", state_path=tmp_path / "state.sqlite3")


# --- restore_state: the four fail-closed outcomes ---------------------------------


def test_state_sync_restore_restores_a_verified_archive_and_reports_restored(
    tmp_path: Path,
) -> None:
    (tmp_path / "source").mkdir()
    source = _make_real_state_files(tmp_path / "source", table_value="authoritative-state")
    client = FakeDriveClient()
    save_state(client=client, folder_id="folder-1", state_path=source, environment="local")
    destination = tmp_path / "destination" / "state.sqlite3"

    outcome = restore_state(
        client=client, folder_id="folder-1", state_path=destination, environment="local"
    )

    assert outcome.restored is True
    assert _read_table_value(destination) == "authoritative-state"


def test_state_sync_restore_reports_absent_on_a_legitimate_first_run(tmp_path: Path) -> None:
    client = FakeDriveClient()
    destination = tmp_path / "state.sqlite3"

    outcome = restore_state(
        client=client, folder_id="folder-1", state_path=destination, environment="local"
    )

    assert outcome.restored is False
    assert not destination.exists()


def test_state_sync_restore_aborts_on_a_digest_mismatch_rather_than_proceeding_empty(
    tmp_path: Path,
) -> None:
    # Simulate corruption or tampering: Drive's recorded digest no longer matches the bytes
    # that actually come back from download.
    filename = state_archive_filename("local")

    class _MismatchedDigestClient(FakeDriveClient):
        def find_file(self, *, folder_id: str, filename: str) -> DriveFile | None:
            found = super().find_file(folder_id=folder_id, filename=filename)
            assert found is not None
            return DriveFile(file_id=found.file_id, sha256="0" * 64)

    tampered_client = _MismatchedDigestClient()
    tampered_client.files[filename] = ("drive-file-1", b"expected content")
    destination = tmp_path / "state.sqlite3"

    with pytest.raises(StateSyncError, match="digest did not verify"):
        restore_state(
            client=tampered_client,
            folder_id="folder-1",
            state_path=destination,
            environment="local",
        )

    assert not destination.exists()


def test_state_sync_restore_aborts_when_download_fails(tmp_path: Path) -> None:
    client = _FailingDownloadClient()
    filename = state_archive_filename("local")
    client.files[filename] = ("drive-file-1", b"some archive bytes")
    destination = tmp_path / "state.sqlite3"

    with pytest.raises(StateSyncError, match="download failed"):
        restore_state(
            client=client, folder_id="folder-1", state_path=destination, environment="local"
        )

    assert not destination.exists()


def test_state_sync_restore_aborts_on_ambiguous_existence_rather_than_treating_it_as_absent(
    tmp_path: Path,
) -> None:
    client = FakeDriveClient(find_file_error=True)
    destination = tmp_path / "state.sqlite3"

    with pytest.raises(StateSyncError, match="determine whether"):
        restore_state(
            client=client, folder_id="folder-1", state_path=destination, environment="local"
        )

    assert not destination.exists()


def test_state_sync_restore_emits_a_distinct_diagnostic_for_absent_versus_restored(
    tmp_path: Path,
) -> None:
    from epub_news_feeder.diagnostics import Diagnostics

    diagnostics_dir = tmp_path / "diagnostics"
    diagnostics = Diagnostics(diagnostics_dir, "run-absent")
    client = FakeDriveClient()

    restore_state(
        client=client,
        folder_id="folder-1",
        state_path=tmp_path / "state.sqlite3",
        environment="local",
        diagnostics=diagnostics,
    )

    text = diagnostics.path.read_text(encoding="utf-8")
    assert "STATE_ABSENT" in text
    assert "STATE_RESTORED" not in text


# --- save_state --------------------------------------------------------------------


def test_state_sync_save_uploads_a_new_archive_when_none_exists(tmp_path: Path) -> None:
    state_path = _make_real_state_files(tmp_path)
    client = FakeDriveClient()

    digest = save_state(
        client=client, folder_id="folder-1", state_path=state_path, environment="local"
    )

    assert client.upload_calls == 1
    assert client.update_calls == 0
    filename = state_archive_filename("local")
    assert filename in client.files
    _, uploaded_bytes = client.files[filename]
    assert sha256(uploaded_bytes).hexdigest() == digest


def test_state_sync_save_overwrites_an_existing_archive_in_place_not_via_a_new_upload(
    tmp_path: Path,
) -> None:
    state_path = _make_real_state_files(tmp_path, table_value="version-one")
    client = FakeDriveClient()
    save_state(client=client, folder_id="folder-1", state_path=state_path, environment="local")
    filename = state_archive_filename("local")
    original_file_id, _ = client.files[filename]

    connection = sqlite3.connect(state_path, isolation_level=None)
    connection.execute("UPDATE t SET value = 'version-two'")
    connection.close()
    save_state(client=client, folder_id="folder-1", state_path=state_path, environment="local")

    assert client.upload_calls == 1
    assert client.update_calls == 1
    updated_file_id, _content = client.files[filename]
    assert updated_file_id == original_file_id


def test_state_sync_save_failure_raises_rather_than_failing_silently(tmp_path: Path) -> None:
    state_path = _make_real_state_files(tmp_path)
    client = _FailingUploadClient()

    with pytest.raises(StateSyncError, match="save failed"):
        save_state(client=client, folder_id="folder-1", state_path=state_path, environment="local")


def test_state_sync_save_checkpoints_the_wal_so_the_archive_is_self_contained(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(state_path, isolation_level=None)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE t(value TEXT)")
    connection.execute("INSERT INTO t VALUES ('committed-before-save')")
    key_path = state_path.with_suffix(".sqlite3.key")
    key_path.write_bytes(b"\x00" * 32)
    client = FakeDriveClient()

    save_state(
        client=client,
        folder_id="folder-1",
        state_path=state_path,
        environment="local",
        connection=connection,
    )
    connection.close()

    destination = tmp_path / "restored-state.sqlite3"
    restore_state(client=client, folder_id="folder-1", state_path=destination, environment="local")
    restored_connection = sqlite3.connect(destination)
    rows = restored_connection.execute("SELECT value FROM t").fetchall()
    restored_connection.close()
    assert rows == [("committed-before-save",)]


# --- roundtrip: save then restore recovers exactly what was saved ------------------


def test_state_sync_save_then_restore_round_trips_through_a_shared_drive_folder(
    tmp_path: Path,
) -> None:
    (tmp_path / "source").mkdir()
    source = _make_real_state_files(tmp_path / "source", table_value="the-authoritative-copy")
    client = FakeDriveClient()

    save_state(client=client, folder_id="folder-1", state_path=source, environment="production")
    destination = tmp_path / "destination" / "state.sqlite3"
    outcome = restore_state(
        client=client, folder_id="folder-1", state_path=destination, environment="production"
    )

    assert outcome.restored is True
    assert _read_table_value(destination) == "the-authoritative-copy"
    assert destination.with_suffix(".sqlite3.key").read_bytes() == (
        source.with_suffix(".sqlite3.key").read_bytes()
    )


# --- security: no secret ever appears in a failed sync's error or diagnostics ------


@pytest.mark.security
def test_state_sync_failed_restore_never_leaks_drive_tokens_in_error_or_diagnostics(
    tmp_path: Path,
) -> None:
    from epub_news_feeder.diagnostics import Diagnostics

    class LeakyClient(FakeDriveClient):
        def find_file(self, *, folder_id: str, filename: str) -> DriveFile | None:
            raise DriveError(
                "Drive lookup failed for refresh token the-refresh-token-value "
                "and access token the-access-token-value"
            )

    diagnostics_dir = tmp_path / "diagnostics"
    diagnostics = Diagnostics(diagnostics_dir, "run-leak")

    with pytest.raises(StateSyncError) as excinfo:
        restore_state(
            client=LeakyClient(),
            folder_id="folder-1",
            state_path=tmp_path / "state.sqlite3",
            environment="local",
            diagnostics=diagnostics,
        )

    error_message = str(excinfo.value)
    assert "the-refresh-token-value" not in error_message
    assert "the-access-token-value" not in error_message

    diagnostics_text = "".join(
        path.read_text(encoding="utf-8") for path in diagnostics_dir.glob("*.jsonl")
    )
    assert "the-refresh-token-value" not in diagnostics_text
    assert "the-access-token-value" not in diagnostics_text


@pytest.mark.security
def test_state_sync_never_writes_the_fingerprint_key_bytes_to_diagnostics(tmp_path: Path) -> None:
    from epub_news_feeder.diagnostics import Diagnostics

    state_path = _make_real_state_files(tmp_path)
    key_bytes = state_path.with_suffix(".sqlite3.key").read_bytes()
    diagnostics_dir = tmp_path / "diagnostics"
    diagnostics = Diagnostics(diagnostics_dir, "run-key")
    client = FakeDriveClient()

    save_state(
        client=client,
        folder_id="folder-1",
        state_path=state_path,
        environment="local",
        diagnostics=diagnostics,
    )
    restore_state(
        client=client,
        folder_id="folder-1",
        state_path=tmp_path / "restored" / "state.sqlite3",
        environment="local",
        diagnostics=diagnostics,
    )

    diagnostics_text = "".join(
        path.read_text(encoding="utf-8") for path in diagnostics_dir.glob("*.jsonl")
    )
    assert key_bytes.hex() not in diagnostics_text
