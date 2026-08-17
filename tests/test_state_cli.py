"""CLI wiring for scheduled State Store persistence: ``state-pull``, ``state-push``, and the
opt-in ``generate --state-folder`` / ``GOOGLE_DRIVE_FOLDER_DB`` automatic restore/save.

No test here touches the network: ``epub_news_feeder.cli.HttpDriveClient`` is monkeypatched to
an in-memory double, matching the existing Drive test convention.
"""

from __future__ import annotations

import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest

from epub_news_feeder import cli
from epub_news_feeder.cli import main
from epub_news_feeder.drive import DriveAuthError, DriveError, DriveFile


class FakeDriveClient:
    """In-memory DriveClient double: no network, shared across calls via a closure."""

    def __init__(self, *, credentials: object = None, fail: bool = False) -> None:
        del credentials
        self.fail = fail
        self.files: dict[str, tuple[str, bytes]] = {}
        self._next_id = 0

    def find_file(self, *, folder_id: str, filename: str) -> DriveFile | None:
        del folder_id
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
        if self.fail:
            raise DriveError("Drive upload failed")
        self._next_id += 1
        file_id = f"drive-file-{self._next_id}"
        self.files[filename] = (file_id, content)
        return file_id

    def update(self, *, file_id: str, content: bytes, content_type: str) -> str:
        del content_type
        if self.fail:
            raise DriveError("Drive update failed")
        for filename, (existing_id, _content) in self.files.items():
            if existing_id == file_id:
                self.files[filename] = (existing_id, content)
                return existing_id
        raise KeyError(file_id)

    def download(self, *, file_id: str) -> bytes:
        if self.fail:
            raise DriveError("Drive download failed")
        for _filename, (existing_id, content) in self.files.items():
            if existing_id == file_id:
                return content
        raise KeyError(file_id)


class _MismatchedDigestClient(FakeDriveClient):
    def find_file(self, *, folder_id: str, filename: str) -> DriveFile | None:
        found = super().find_file(folder_id=folder_id, filename=filename)
        if found is None:
            return None
        return DriveFile(file_id=found.file_id, sha256="0" * 64)


def _set_oauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "the-client-secret-value")
    monkeypatch.setenv("GOOGLE_OAUTH_REFRESH_TOKEN", "the-refresh-token-value")


def _make_real_state_files(state_path: Path, *, table_value: str = "sample-value") -> None:
    connection = sqlite3.connect(state_path, isolation_level=None)
    try:
        connection.execute("CREATE TABLE t(value TEXT)")
        connection.execute("INSERT INTO t VALUES (?)", (table_value,))
    finally:
        connection.close()
    key_path = state_path.with_suffix(".sqlite3.key")
    key_path.write_bytes(b"\x00" * 32)
    key_path.chmod(0o600)


# --- state-push --------------------------------------------------------------------


def test_state_push_saves_the_state_store_and_reports_its_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_oauth_env(monkeypatch)
    client = FakeDriveClient()
    monkeypatch.setattr(cli, "HttpDriveClient", lambda *, credentials: client)
    state_path = tmp_path / "state.sqlite3"
    _make_real_state_files(state_path)

    exit_code = main(
        [
            "state-push",
            "--state",
            str(state_path),
            "--state-folder",
            "folder-db",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "code=STATE_SAVED" in captured.out
    assert "state-local.tar.gz" in client.files


def test_state_push_reports_a_clear_failure_without_leaking_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_oauth_env(monkeypatch)
    client = FakeDriveClient(fail=True)
    monkeypatch.setattr(cli, "HttpDriveClient", lambda *, credentials: client)
    state_path = tmp_path / "state.sqlite3"
    _make_real_state_files(state_path)

    exit_code = main(["state-push", "--state", str(state_path), "--state-folder", "folder-db"])

    assert exit_code == 3
    captured = capsys.readouterr()
    assert "code=STATE_SAVE_FAILED" in captured.err
    assert "the-refresh-token-value" not in captured.err


# --- state-pull --------------------------------------------------------------------


def test_state_pull_restores_a_previously_pushed_state_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_oauth_env(monkeypatch)
    client = FakeDriveClient()
    monkeypatch.setattr(cli, "HttpDriveClient", lambda *, credentials: client)
    source_state = tmp_path / "source" / "state.sqlite3"
    source_state.parent.mkdir()
    _make_real_state_files(source_state, table_value="pushed-value")
    assert main(["state-push", "--state", str(source_state), "--state-folder", "folder-db"]) == 0
    capsys.readouterr()

    destination_state = tmp_path / "destination" / "state.sqlite3"
    exit_code = main(
        ["state-pull", "--state", str(destination_state), "--state-folder", "folder-db"]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "code=STATE_RESTORED" in captured.out
    with sqlite3.connect(destination_state) as connection:
        assert connection.execute("SELECT value FROM t").fetchone() == ("pushed-value",)


def test_state_pull_reports_absent_on_a_clean_first_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_oauth_env(monkeypatch)
    client = FakeDriveClient()
    monkeypatch.setattr(cli, "HttpDriveClient", lambda *, credentials: client)
    state_path = tmp_path / "state.sqlite3"

    exit_code = main(["state-pull", "--state", str(state_path), "--state-folder", "folder-db"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "code=STATE_ABSENT" in captured.out
    assert not state_path.exists()


def test_state_pull_fails_closed_on_a_digest_mismatch_without_leaking_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_oauth_env(monkeypatch)
    client = _MismatchedDigestClient()
    client.files["state-local.tar.gz"] = ("drive-file-1", b"tampered bytes")
    monkeypatch.setattr(cli, "HttpDriveClient", lambda *, credentials: client)
    state_path = tmp_path / "state.sqlite3"

    exit_code = main(["state-pull", "--state", str(state_path), "--state-folder", "folder-db"])

    assert exit_code == 3
    captured = capsys.readouterr()
    assert "code=STATE_RESTORE_FAILED" in captured.err
    assert "the-refresh-token-value" not in captured.err
    assert not state_path.exists()


def test_state_pull_names_a_rejected_refresh_token_rather_than_an_unverifiable_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_oauth_env(monkeypatch)

    class _RejectedCredentialsClient(FakeDriveClient):
        def find_file(self, *, folder_id: str, filename: str) -> DriveFile | None:
            raise DriveAuthError("Google rejected the Drive credentials (invalid_grant)")

    client = _RejectedCredentialsClient()
    monkeypatch.setattr(cli, "HttpDriveClient", lambda *, credentials: client)

    exit_code = main(
        ["state-pull", "--state", str(tmp_path / "state.sqlite3"), "--state-folder", "folder-db"]
    )

    assert exit_code == 3
    captured = capsys.readouterr()
    assert "code=DRIVE_AUTH_FAILED" in captured.err
    assert "authorize-drive" in captured.err
    assert "the-refresh-token-value" not in captured.err


@pytest.mark.security
def test_state_pull_missing_drive_credentials_reports_without_leaking_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_REFRESH_TOKEN", raising=False)

    exit_code = main(
        [
            "state-pull",
            "--state",
            str(tmp_path / "state.sqlite3"),
            "--state-folder",
            "folder-db",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "code=DRIVE_CONFIGURATION_INVALID" in captured.err
    assert "GOOGLE_OAUTH_CLIENT_ID" in captured.err


# --- generate --state-folder / GOOGLE_DRIVE_FOLDER_DB wiring ------------------------


def _config(tmp_path: Path) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        """version: 1
sources:
  source:
    title: Source
    feed_url: https://example.com/feed.xml
publications:
  - id: daily
    title: Daily
    sections:
      - id: news
        title: News
        sources: [source]
""",
        encoding="utf-8",
    )
    return config


def test_generate_reports_missing_drive_credentials_when_only_state_folder_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_REFRESH_TOKEN", raising=False)

    exit_code = main(
        [
            "generate",
            "--config",
            str(_config(tmp_path)),
            "--state",
            str(tmp_path / "state.sqlite3"),
            "--output",
            str(tmp_path / "output"),
            "--state-folder",
            "folder-db",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "code=DRIVE_CONFIGURATION_INVALID" in captured.err


def test_generate_state_sync_defaults_to_the_google_drive_folder_db_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unset ``--state-folder``, only the ``GOOGLE_DRIVE_FOLDER_DB`` env var: still wires in."""

    _set_oauth_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_DB", "folder-db-from-env")
    client = FakeDriveClient()
    monkeypatch.setattr(cli, "HttpDriveClient", lambda *, credentials: client)

    exit_code = main(
        [
            "generate",
            "--config",
            str(_config(tmp_path)),
            "--state",
            str(tmp_path / "state.sqlite3"),
            "--output",
            str(tmp_path / "output"),
        ]
    )

    # The Publication has no eligible Source evidence, so generation itself fails — but the
    # State Store restore step must already have run and reported absence via the env var.
    assert exit_code == 3
    diagnostics_text = next((tmp_path / "diagnostics").glob("*.jsonl")).read_text()
    assert "STATE_ABSENT" in diagnostics_text


def test_generate_without_state_folder_configured_behaves_exactly_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GOOGLE_DRIVE_FOLDER_DB", raising=False)
    monkeypatch.delenv("GOOGLE_DRIVE_FOLDER_ID", raising=False)

    exit_code = main(
        [
            "generate",
            "--config",
            str(_config(tmp_path)),
            "--state",
            str(tmp_path / "state.sqlite3"),
            "--output",
            str(tmp_path / "output"),
        ]
    )

    assert exit_code == 3
    assert (
        not (tmp_path / "diagnostics" / "outcomes.ndjson").exists()
        or "STATE_" not in (tmp_path / "diagnostics" / "outcomes.ndjson").read_text()
    )
