"""Wiring of scheduled State Store persistence (Drive) into ``generate_edition``.

A GitHub-hosted runner starts with an empty filesystem between scheduled runs. These tests
model that directly: each simulated "run" uses its own fresh ``tmp_path`` subdirectory for
state/output/diagnostics, while a single in-memory ``FakeDriveClient`` stands in for the one
persistent, private Drive folder shared across runs.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest

from epub_news_feeder.application import (
    GenerationError,
    RetryableGenerationError,
    StateSyncTarget,
    generate_edition,
)
from epub_news_feeder.config import load_config
from epub_news_feeder.drive import DriveAuthError, DriveError, DriveFile
from epub_news_feeder.models import Configuration


class FakeDriveClient:
    """In-memory DriveClient double standing in for one persistent, private Drive folder."""

    def __init__(
        self,
        *,
        fail_find: bool = False,
        fail_download: bool = False,
        fail_upload: bool = False,
    ) -> None:
        self.files: dict[str, tuple[str, bytes]] = {}
        self._next_id = 0
        self.upload_calls = 0
        self.update_calls = 0
        self.download_calls = 0
        self._fail_find = fail_find
        self._fail_download = fail_download
        self._fail_upload = fail_upload

    def find_file(self, *, folder_id: str, filename: str) -> DriveFile | None:
        del folder_id
        if self._fail_find:
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
        if self._fail_upload:
            raise DriveError("Drive upload failed")
        self.upload_calls += 1
        self._next_id += 1
        file_id = f"drive-file-{self._next_id}"
        self.files[filename] = (file_id, content)
        return file_id

    def update(self, *, file_id: str, content: bytes, content_type: str) -> str:
        del content_type
        if self._fail_upload:
            raise DriveError("Drive update failed")
        self.update_calls += 1
        for filename, (existing_id, _content) in self.files.items():
            if existing_id == file_id:
                self.files[filename] = (existing_id, content)
                return existing_id
        raise KeyError(file_id)

    def download(self, *, file_id: str) -> bytes:
        if self._fail_download:
            raise DriveError("Drive download failed")
        self.download_calls += 1
        for _filename, (existing_id, content) in self.files.items():
            if existing_id == file_id:
                return content
        raise KeyError(file_id)


class _MismatchedDigestClient(FakeDriveClient):
    """A Drive folder whose recorded digest no longer matches the bytes it serves."""

    def find_file(self, *, folder_id: str, filename: str) -> DriveFile | None:
        found = super().find_file(folder_id=folder_id, filename=filename)
        if found is None:
            return None
        return DriveFile(file_id=found.file_id, sha256="0" * 64)


class _OneBriefFixtureHandler(BaseHTTPRequestHandler):
    """Serves the same single, stable Brief on every request."""

    def do_GET(self) -> None:
        if self.path == "/robots.txt":
            content_type, payload, status = "text/plain", b"User-agent: *\nAllow: /\n", 200
        elif self.path == "/briefs.xml":
            content_type = "application/rss+xml"
            server = cast(ThreadingHTTPServer, self.server)
            origin = f"http://127.0.0.1:{server.server_port}"
            payload = f"""<rss version="2.0"><channel><title>Ekot Fixture</title>
<item><title>Coastal watch issued</title><link>{origin}/brief/coastal-watch</link>
<guid>brief-coastal-watch</guid><pubDate>Sun, 09 Aug 2026 06:00:00 GMT</pubDate></item>
</channel></rss>""".encode()
            status = 200
        else:
            content_type, payload, status = "text/plain", b"not found", 404
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def _fixture_configuration(tmp_path: Path) -> Iterator[tuple[Configuration, ThreadingHTTPServer]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OneBriefFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config_path = tmp_path / "publication.yaml"
    config_path.write_text(
        f"""
version: 1
sources:
  briefs:
    title: Sveriges Radio Ekot
    publisher_id: fixture-ekot
    feed_url: http://127.0.0.1:{server.server_port}/briefs.xml
    acquisition: metadata_only
    llm_processing: disabled
    rights:
      basis: link-only
      audience: single_operator
      attribution_required: true
      media_reuse: false
    eligibility:
      evidence_reviewed_at: 2026-08-09
      review_expires_at: 2026-09-08
      evidence_id: brief-fixture
      feed_acquisition: allow
      page_acquisition: deny
      retention: deny
      private_distribution: conditional
      local_llm: deny
      remote_llm: deny
publications:
  - id: daily
    title: Daily Edition
    language: en
    budget: {{max_articles: 6, min_articles: 0}}
    max_briefs: 6
    sections:
      - id: current
        title: Current reporting
        sources: [briefs]
""".lstrip(),
        encoding="utf-8",
    )
    configuration = load_config(config_path)
    try:
        yield configuration, server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _generate(
    tmp_path: Path,
    label: str,
    configuration: Configuration,
    *,
    run_id: str,
    state_sync_target: StateSyncTarget | None,
) -> Path:
    """Run generate_edition against a fresh subdirectory, modeling an empty runner disk."""

    return generate_edition(
        configuration,
        state_path=tmp_path / label / "state.sqlite3",
        output_directory=tmp_path / label / "editions",
        diagnostics_directory=tmp_path / label / "diagnostics",
        run_id=run_id,
        generated_at=datetime(2026, 8, 9, 6, tzinfo=UTC),
        state_sync_target=state_sync_target,
    ).receipt.path.parent


# --- absent archive: a legitimate first run -----------------------------------------


def test_state_sync_absent_archive_is_a_legitimate_first_run_and_emits_a_distinct_diagnostic(
    tmp_path: Path, _fixture_configuration: tuple[Configuration, ThreadingHTTPServer]
) -> None:
    configuration, _server = _fixture_configuration
    client = FakeDriveClient()

    _generate(
        tmp_path,
        "run1",
        configuration,
        run_id="20260809T060000Z-FIRSTRUNA",
        state_sync_target=StateSyncTarget(
            client=client, folder_id="state-folder", environment="ci"
        ),
    )

    diagnostics_text = (
        tmp_path / "run1" / "diagnostics" / "20260809T060000Z-FIRSTRUNA.jsonl"
    ).read_text()
    assert "STATE_ABSENT" in diagnostics_text
    assert "STATE_RESTORED" not in diagnostics_text
    # The successful, finalized delivery was then saved to Drive.
    assert client.upload_calls == 1


# --- restore then reuse: the scenario issue #60 exists to prevent ------------------


def test_state_sync_restores_across_two_runs_on_separate_empty_filesystems_and_suppresses_repeats(
    tmp_path: Path, _fixture_configuration: tuple[Configuration, ThreadingHTTPServer]
) -> None:
    configuration, _server = _fixture_configuration
    client = FakeDriveClient()
    target = StateSyncTarget(client=client, folder_id="state-folder", environment="ci")

    first_result = generate_edition(
        configuration,
        state_path=tmp_path / "run1" / "state.sqlite3",
        output_directory=tmp_path / "run1" / "editions",
        diagnostics_directory=tmp_path / "run1" / "diagnostics",
        run_id="20260809T060000Z-RUNONEAAA",
        generated_at=datetime(2026, 8, 9, 6, tzinfo=UTC),
        state_sync_target=target,
    )
    assert first_result.brief_count == 1
    assert client.upload_calls == 1

    # A brand-new "run2" subdirectory: no local state.sqlite3, no key sidecar, nothing —
    # exactly what a fresh GitHub-hosted runner sees, but the same Drive folder is shared.
    second_result = generate_edition(
        configuration,
        state_path=tmp_path / "run2" / "state.sqlite3",
        output_directory=tmp_path / "run2" / "editions",
        diagnostics_directory=tmp_path / "run2" / "diagnostics",
        run_id="20260809T070000Z-RUNTWOAAA",
        generated_at=datetime(2026, 8, 9, 7, tzinfo=UTC),
        state_sync_target=target,
    )

    # The identical Brief was already delivered in run1 and restored via Drive: it is
    # suppressed rather than re-delivered, which is exactly the defect issue #60 fixed.
    assert second_result.brief_count == 0
    diagnostics_text = (
        tmp_path / "run2" / "diagnostics" / "20260809T070000Z-RUNTWOAAA.jsonl"
    ).read_text()
    assert "STATE_RESTORED" in diagnostics_text


# --- fail-closed restore outcomes ---------------------------------------------------


def test_state_sync_restore_aborts_the_run_on_a_digest_mismatch(
    tmp_path: Path, _fixture_configuration: tuple[Configuration, ThreadingHTTPServer]
) -> None:
    configuration, _server = _fixture_configuration
    client = _MismatchedDigestClient()
    filename = "state-ci.tar.gz"
    client.files[filename] = ("drive-file-1", b"tampered archive bytes")
    state_path = tmp_path / "state.sqlite3"

    with pytest.raises(GenerationError, match="could not be verified"):
        generate_edition(
            configuration,
            state_path=state_path,
            output_directory=tmp_path / "editions",
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260809T060000Z-MISMATCHA",
            generated_at=datetime(2026, 8, 9, 6, tzinfo=UTC),
            state_sync_target=StateSyncTarget(
                client=client, folder_id="state-folder", environment="ci"
            ),
        )

    # Never proceeds with an empty store: the local State Store was never even opened.
    assert not state_path.exists()
    assert not (tmp_path / "editions").exists()


def test_state_sync_restore_aborts_the_run_when_download_fails(
    tmp_path: Path, _fixture_configuration: tuple[Configuration, ThreadingHTTPServer]
) -> None:
    configuration, _server = _fixture_configuration
    client = FakeDriveClient(fail_download=True)
    filename = "state-ci.tar.gz"
    client.files[filename] = ("drive-file-1", b"some archive bytes")
    state_path = tmp_path / "state.sqlite3"

    with pytest.raises(GenerationError, match="could not be verified"):
        generate_edition(
            configuration,
            state_path=state_path,
            output_directory=tmp_path / "editions",
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260809T060000Z-DOWNFAILA",
            generated_at=datetime(2026, 8, 9, 6, tzinfo=UTC),
            state_sync_target=StateSyncTarget(
                client=client, folder_id="state-folder", environment="ci"
            ),
        )

    assert not state_path.exists()


def test_state_sync_restore_aborts_the_run_on_an_ambiguous_existence_check(
    tmp_path: Path, _fixture_configuration: tuple[Configuration, ThreadingHTTPServer]
) -> None:
    configuration, _server = _fixture_configuration
    client = FakeDriveClient(fail_find=True)
    state_path = tmp_path / "state.sqlite3"

    with pytest.raises(GenerationError, match="could not be verified"):
        generate_edition(
            configuration,
            state_path=state_path,
            output_directory=tmp_path / "editions",
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260809T060000Z-AMBIGUOUSA",
            generated_at=datetime(2026, 8, 9, 6, tzinfo=UTC),
            state_sync_target=StateSyncTarget(
                client=client, folder_id="state-folder", environment="ci"
            ),
        )

    assert not state_path.exists()


# --- save is loud on failure, and never abandons an already-finalized Run ----------


def test_state_sync_save_failure_is_retryable_and_does_not_abandon_the_finalized_run(
    tmp_path: Path, _fixture_configuration: tuple[Configuration, ThreadingHTTPServer]
) -> None:
    configuration, _server = _fixture_configuration
    client = FakeDriveClient(fail_upload=True)
    state_path = tmp_path / "state.sqlite3"
    output_directory = tmp_path / "editions"

    with pytest.raises(RetryableGenerationError, match="save to Drive remains pending"):
        generate_edition(
            configuration,
            state_path=state_path,
            output_directory=output_directory,
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260809T060000Z-SAVEFAILA",
            generated_at=datetime(2026, 8, 9, 6, tzinfo=UTC),
            state_sync_target=StateSyncTarget(
                client=client, folder_id="state-folder", environment="ci"
            ),
        )

    # Local delivery and finalization already succeeded and must not be undone.
    with sqlite3.connect(state_path) as connection:
        assert connection.execute("SELECT status FROM runs").fetchone() == ("delivered",)
    assert len(list(output_directory.glob("*.epub"))) == 1


# --- security: no secret ever appears in a failed sync's error or diagnostics ------


@pytest.mark.security
def test_state_sync_restore_failure_never_leaks_drive_tokens_in_error_or_diagnostics(
    tmp_path: Path, _fixture_configuration: tuple[Configuration, ThreadingHTTPServer]
) -> None:
    configuration, _server = _fixture_configuration
    diagnostics_directory = tmp_path / "diagnostics"

    class LeakyClient(FakeDriveClient):
        def find_file(self, *, folder_id: str, filename: str) -> DriveFile | None:
            del folder_id, filename
            raise DriveError(
                "Drive lookup failed for refresh token the-refresh-token-value "
                "and access token the-access-token-value"
            )

    with pytest.raises(GenerationError) as excinfo:
        generate_edition(
            configuration,
            state_path=tmp_path / "state.sqlite3",
            output_directory=tmp_path / "editions",
            diagnostics_directory=diagnostics_directory,
            run_id="20260809T060000Z-LEAKCHECK",
            generated_at=datetime(2026, 8, 9, 6, tzinfo=UTC),
            state_sync_target=StateSyncTarget(
                client=LeakyClient(), folder_id="state-folder", environment="ci"
            ),
        )

    error_message = str(excinfo.value)
    assert "the-refresh-token-value" not in error_message
    assert "the-access-token-value" not in error_message
    diagnostics_text = "".join(
        path.read_text(encoding="utf-8") for path in diagnostics_directory.glob("*.jsonl")
    )
    assert "the-refresh-token-value" not in diagnostics_text
    assert "the-access-token-value" not in diagnostics_text


# --- a rejected refresh token says so, rather than reading as an unverifiable archive


class _RejectedCredentialsClient(FakeDriveClient):
    """Every call fails the way an expired or revoked refresh token fails."""

    def find_file(self, *, folder_id: str, filename: str) -> DriveFile | None:
        raise DriveAuthError("Google rejected the Drive credentials (invalid_grant)")


def test_rejected_drive_credentials_abort_the_run_naming_the_credential_not_the_archive(
    tmp_path: Path, _fixture_configuration: tuple[Configuration, ThreadingHTTPServer]
) -> None:
    configuration, _server = _fixture_configuration
    state_path = tmp_path / "state.sqlite3"
    diagnostics_directory = tmp_path / "diagnostics"

    with pytest.raises(GenerationError) as excinfo:
        generate_edition(
            configuration,
            state_path=state_path,
            output_directory=tmp_path / "editions",
            diagnostics_directory=diagnostics_directory,
            run_id="20260809T060000Z-REJECTEDA",
            generated_at=datetime(2026, 8, 9, 6, tzinfo=UTC),
            state_sync_target=StateSyncTarget(
                client=_RejectedCredentialsClient(), folder_id="state-folder", environment="ci"
            ),
        )

    assert excinfo.value.code == "DRIVE_AUTH_FAILED"
    assert "refresh token" in excinfo.value.safe_message
    assert "authorize-drive" in excinfo.value.safe_message
    diagnostics_text = "".join(
        path.read_text(encoding="utf-8") for path in diagnostics_directory.glob("*.jsonl")
    )
    assert '"code":"DRIVE_AUTH_FAILED"' in diagnostics_text
    assert '"code":"STATE_RESTORE_FAILED"' not in diagnostics_text
    # Still fail-closed: nothing was delivered from an empty State Store.
    assert not state_path.exists()


def test_rejected_drive_credentials_at_save_time_do_not_abandon_the_finalized_run(
    tmp_path: Path, _fixture_configuration: tuple[Configuration, ThreadingHTTPServer]
) -> None:
    configuration, _server = _fixture_configuration
    state_path = tmp_path / "state.sqlite3"
    output_directory = tmp_path / "editions"

    class _RejectedOnSaveClient(FakeDriveClient):
        """Restore succeeds (a clean first run); the credential dies before the save."""

        def __init__(self) -> None:
            super().__init__()
            self.restored = False

        def find_file(self, *, folder_id: str, filename: str) -> DriveFile | None:
            if not self.restored:
                self.restored = True
                return None
            raise DriveAuthError("Google rejected the Drive credentials (invalid_grant)")

    with pytest.raises(RetryableGenerationError) as excinfo:
        generate_edition(
            configuration,
            state_path=state_path,
            output_directory=output_directory,
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260809T060000Z-REJECTEDB",
            generated_at=datetime(2026, 8, 9, 6, tzinfo=UTC),
            state_sync_target=StateSyncTarget(
                client=_RejectedOnSaveClient(), folder_id="state-folder", environment="ci"
            ),
        )

    assert excinfo.value.code == "DRIVE_AUTH_FAILED"
    with sqlite3.connect(state_path) as connection:
        assert connection.execute("SELECT status FROM runs").fetchone() == ("delivered",)
    assert len(list(output_directory.glob("*.epub"))) == 1
