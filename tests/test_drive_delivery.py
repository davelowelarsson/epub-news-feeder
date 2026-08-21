from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from epub_news_feeder.application import DriveTarget, RetryableGenerationError, generate_edition
from epub_news_feeder.config import load_config
from epub_news_feeder.drive import DriveError, DriveFile, DriveFolderEntry
from epub_news_feeder.models import Configuration


class FakeDriveClient:
    """In-memory DriveClient double: no network, immutable-copy semantics like Drive itself."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.files: dict[str, tuple[str, bytes]] = {}
        self.upload_calls = 0
        self.list_calls = 0
        self.moves: list[tuple[str, str, str]] = []

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
        self.upload_calls += 1
        if self.fail:
            raise DriveError("Drive upload failed")
        file_id = f"drive-file-{self.upload_calls}"
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

    def list_folder(self, *, folder_id: str) -> tuple[DriveFolderEntry, ...]:
        self.list_calls += 1
        return tuple(
            DriveFolderEntry(file_id=existing_id, name=filename)
            for filename, (existing_id, _content) in self.files.items()
        )

    def move(self, *, file_id: str, from_folder_id: str, to_folder_id: str) -> str:
        self.moves.append((file_id, from_folder_id, to_folder_id))
        return file_id


class _FixtureHandler(BaseHTTPRequestHandler):
    body = " ".join(f"complete-journalism-{index}" for index in range(180))

    def do_GET(self) -> None:
        if self.path == "/robots.txt":
            content_type, payload, status = "text/plain", b"User-agent: *\nAllow: /\n", 200
        elif self.path == "/feed.xml":
            content_type = "application/rss+xml"
            payload = f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Fixture News</title><item>
<title>A complete local report</title>
<link>https://publisher.example/reports/complete</link>
<guid>fixture-complete-1</guid><author>A. Reporter</author>
<description>This preview is discovery metadata only.</description>
<content:encoded><![CDATA[<p>{type(self).body}</p>]]></content:encoded>
</item></channel></rss>""".encode()
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
def _fixture_configuration(
    tmp_path: Path,
) -> Iterator[tuple[Configuration, ThreadingHTTPServer]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config_path = tmp_path / "publication.yaml"
    config_path.write_text(
        f"""
version: 1
sources:
  fixture:
    title: Fixture News
    publisher_id: fixture-publisher
    allowed_publisher_origins: [https://publisher.example]
    feed_url: http://127.0.0.1:{server.server_port}/feed.xml
    acquisition: feed
    llm_processing: local_only
    rights:
      basis: fixture_private_use
      audience: single_operator
      attribution_required: true
      media_reuse: false
    eligibility:
      evidence_reviewed_at: 2026-08-09
      review_expires_at: 2026-09-08
      evidence_id: fixture-20260809
      feed_acquisition: allow
      page_acquisition: allow
      retention: allow
      private_distribution: allow
      local_llm: allow
      remote_llm: unknown
publications:
  - id: morning
    title: Morning Briefing
    language: en
    budget: {{max_articles: 2, min_articles: 1}}
    sections:
      - id: world
        title: World
        sources: [fixture]
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


@pytest.mark.acceptance
@pytest.mark.epubcheck
def test_configured_drive_target_receives_the_delivered_edition_as_well(
    tmp_path: Path, _fixture_configuration: tuple[Configuration, ThreadingHTTPServer]
) -> None:
    configuration, _server = _fixture_configuration
    drive_client = FakeDriveClient()
    drive_target = DriveTarget(client=drive_client, folder_id="folder-1")

    result = generate_edition(
        configuration,
        state_path=tmp_path / "state.sqlite3",
        output_directory=tmp_path / "editions",
        diagnostics_directory=tmp_path / "diagnostics",
        run_id="20260809T070000Z-CCCCCCCC",
        generated_at=datetime(2026, 8, 9, 7, tzinfo=UTC),
        drive_target=drive_target,
    )

    assert drive_client.upload_calls == 1
    uploaded_filename, (_file_id, uploaded_bytes) = next(iter(drive_client.files.items()))
    assert uploaded_filename == result.receipt.path.name
    assert uploaded_bytes == result.receipt.path.read_bytes()


@pytest.mark.acceptance
@pytest.mark.epubcheck
def test_drive_delivery_failure_is_retryable_and_local_delivery_is_not_abandoned(
    tmp_path: Path, _fixture_configuration: tuple[Configuration, ThreadingHTTPServer]
) -> None:
    configuration, _server = _fixture_configuration
    state_path = tmp_path / "state.sqlite3"
    output_directory = tmp_path / "editions"
    run_id = "20260809T070000Z-DDDDDDDD"
    generated_at = datetime(2026, 8, 9, 7, tzinfo=UTC)
    failing_client = FakeDriveClient(fail=True)

    with pytest.raises(RetryableGenerationError, match="Drive Delivery remains pending"):
        generate_edition(
            configuration,
            state_path=state_path,
            output_directory=output_directory,
            diagnostics_directory=tmp_path / "diagnostics",
            run_id=run_id,
            generated_at=generated_at,
            drive_target=DriveTarget(client=failing_client, folder_id="folder-1"),
        )

    # Local delivery already completed and the Run was finalized; it must not be abandoned.
    with sqlite3.connect(state_path) as connection:
        assert connection.execute("SELECT status FROM runs").fetchone() == ("delivered",)
    delivered_files = list(output_directory.iterdir())
    assert len(delivered_files) == 1

    recovered_client = FakeDriveClient()
    result = generate_edition(
        configuration,
        state_path=state_path,
        output_directory=output_directory,
        diagnostics_directory=tmp_path / "diagnostics",
        run_id=run_id,
        generated_at=generated_at,
        drive_target=DriveTarget(client=recovered_client, folder_id="folder-1"),
    )

    assert recovered_client.upload_calls == 1
    assert result.receipt.path == delivered_files[0]


@pytest.mark.acceptance
@pytest.mark.epubcheck
def test_expired_editions_are_archived_after_a_successful_delivery(
    tmp_path: Path, _fixture_configuration: tuple[Configuration, ThreadingHTTPServer]
) -> None:
    configuration, _server = _fixture_configuration
    drive_client = FakeDriveClient()
    drive_client.files["2026-07-20-morning-ABCDEFGH.epub"] = (drive_file_id := "old-edition", b"")
    drive_client.files["2026-08-08-morning-IJKLMNOP.epub"] = ("fresh-edition", b"")
    drive_client.files["state-production.tar.gz"] = ("state-tarball", b"")

    generate_edition(
        configuration,
        state_path=tmp_path / "state.sqlite3",
        output_directory=tmp_path / "editions",
        diagnostics_directory=tmp_path / "diagnostics",
        run_id="20260809T070000Z-FFFFFFFF",
        generated_at=datetime(2026, 8, 9, 7, tzinfo=UTC),
        drive_target=DriveTarget(
            client=drive_client, folder_id="folder-1", archive_folder_id="archive-1"
        ),
    )

    assert drive_client.moves == [(drive_file_id, "folder-1", "archive-1")]


@pytest.mark.acceptance
@pytest.mark.epubcheck
def test_no_archive_folder_means_no_housekeeping(
    tmp_path: Path, _fixture_configuration: tuple[Configuration, ThreadingHTTPServer]
) -> None:
    configuration, _server = _fixture_configuration
    drive_client = FakeDriveClient()
    drive_client.files["2026-07-20-morning-ABCDEFGH.epub"] = ("old-edition", b"")

    generate_edition(
        configuration,
        state_path=tmp_path / "state.sqlite3",
        output_directory=tmp_path / "editions",
        diagnostics_directory=tmp_path / "diagnostics",
        run_id="20260809T070000Z-GGGGGGGG",
        generated_at=datetime(2026, 8, 9, 7, tzinfo=UTC),
        drive_target=DriveTarget(client=drive_client, folder_id="folder-1"),
    )

    assert drive_client.list_calls == 0
    assert drive_client.moves == []


@pytest.mark.acceptance
@pytest.mark.epubcheck
def test_a_failed_archive_pass_never_fails_a_delivered_run(
    tmp_path: Path, _fixture_configuration: tuple[Configuration, ThreadingHTTPServer]
) -> None:
    """Housekeeping runs after delivery; the Edition is on the device either way."""

    configuration, _server = _fixture_configuration
    diagnostics_directory = tmp_path / "diagnostics"

    class FailingListClient(FakeDriveClient):
        def list_folder(self, *, folder_id: str) -> tuple[DriveFolderEntry, ...]:
            raise DriveError("Drive folder listing failed")

    result = generate_edition(
        configuration,
        state_path=tmp_path / "state.sqlite3",
        output_directory=tmp_path / "editions",
        diagnostics_directory=diagnostics_directory,
        run_id="20260809T070000Z-HHHHHHHH",
        generated_at=datetime(2026, 8, 9, 7, tzinfo=UTC),
        drive_target=DriveTarget(
            client=FailingListClient(), folder_id="folder-1", archive_folder_id="archive-1"
        ),
    )

    assert result.article_count >= 1
    diagnostics_text = "".join(
        path.read_text(encoding="utf-8") for path in diagnostics_directory.glob("*.jsonl")
    )
    assert "DRIVE_ARCHIVE_FAILED" in diagnostics_text


@pytest.mark.security
def test_failed_drive_delivery_never_leaks_tokens_in_diagnostics_or_the_error(
    tmp_path: Path, _fixture_configuration: tuple[Configuration, ThreadingHTTPServer]
) -> None:
    configuration, _server = _fixture_configuration
    diagnostics_directory = tmp_path / "diagnostics"

    class LeakyFailingClient(FakeDriveClient):
        def upload(
            self,
            *,
            folder_id: str,
            filename: str,
            content: bytes,
            content_type: str = "application/epub+zip",
        ) -> str:
            del folder_id, filename, content, content_type
            raise DriveError(
                "Drive upload failed for refresh token the-refresh-token-value "
                "and access token the-access-token-value"
            )

    with pytest.raises(RetryableGenerationError) as excinfo:
        generate_edition(
            configuration,
            state_path=tmp_path / "state.sqlite3",
            output_directory=tmp_path / "editions",
            diagnostics_directory=diagnostics_directory,
            run_id="20260809T070000Z-EEEEEEEE",
            generated_at=datetime(2026, 8, 9, 7, tzinfo=UTC),
            drive_target=DriveTarget(client=LeakyFailingClient(), folder_id="folder-1"),
        )

    error_message = str(excinfo.value)
    assert "the-refresh-token-value" not in error_message
    assert "the-access-token-value" not in error_message

    diagnostics_text = "".join(
        path.read_text(encoding="utf-8") for path in diagnostics_directory.glob("*.jsonl")
    )
    assert "the-refresh-token-value" not in diagnostics_text
    assert "the-access-token-value" not in diagnostics_text
