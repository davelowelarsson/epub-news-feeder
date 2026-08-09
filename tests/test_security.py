from __future__ import annotations

import os
import socket
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from epub_news_feeder.acquisition import (
    AcquisitionMode,
    EligibilityEvidence,
    SourceClient,
    SourceRequest,
)
from epub_news_feeder.diagnostics import Diagnostics
from epub_news_feeder.state import StateStore
from epub_news_feeder.validation import EpubValidationError, validate_epub


@pytest.mark.security
def test_ticket_05_dns_rebinding_is_rejected_before_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def rebind(
        host: str, port: int | None, *, type: socket.SocketKind
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
        nonlocal calls
        calls += 1
        address = "93.184.216.34" if calls == 1 else "127.0.0.1"
        return [(socket.AF_INET, type, socket.IPPROTO_TCP, "", (address, port or 80))]

    monkeypatch.setattr(socket, "getaddrinfo", rebind)
    now = datetime(2026, 8, 9, tzinfo=UTC)
    outcome = SourceClient(now=lambda: now, max_attempts=1).acquire(
        SourceRequest(
            source_id="rebind",
            publisher_id="publisher.example",
            title="Rebinding source",
            feed_url="http://rebind.example/feed.xml",
            mode=AcquisitionMode.FEED,
            llm_processing="local_only",
            evidence=EligibilityEvidence(
                evidence_id="fixture",
                reviewed_at=now - timedelta(days=1),
                expires_at=now + timedelta(days=1),
                feed_acquisition="allow",
                page_acquisition="allow",
                retention="allow",
                private_distribution="allow",
                local_llm="allow",
                remote_llm="deny",
            ),
        )
    )

    assert outcome.code == "SOURCE_ROBOTS_UNAVAILABLE"
    assert outcome.articles == ()
    assert calls == 2


@pytest.mark.security
def test_ticket_11_tampered_epubcheck_is_rejected_before_execution(tmp_path: Path) -> None:
    tampered = tmp_path / "epubcheck.jar"
    tampered.write_bytes(b"not the reviewed EPUBCheck binary")

    with pytest.raises(EpubValidationError, match=r"reviewed EPUBCheck 5\.3\.0"):
        validate_epub(b"not relevant", jar_path=tampered)


@pytest.mark.security
@pytest.mark.acceptance
def test_ticket_12_diagnostics_allowlist_and_private_retention(tmp_path: Path) -> None:
    old = tmp_path / "20000101T000000Z-AAAAAAAA.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    old_time = (datetime.now(UTC) - timedelta(days=91)).timestamp()
    os.utime(old, (old_time, old_time))

    diagnostics = Diagnostics(tmp_path, "20260809T060000Z-BBBBBBBB")
    diagnostics.emit("RUN_STARTED", phase="run", publication_id="daily")

    with pytest.raises(ValueError, match="Diagnostic field is not allowlisted"):
        diagnostics.emit("UNSAFE", phase="run", body="private journalism")

    assert not old.exists()
    assert diagnostics.path.stat().st_mode & 0o077 == 0


@pytest.mark.security
def test_ticket_12_writer_contention_is_value_free_at_cli_boundary(tmp_path: Path) -> None:
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
    state_path = tmp_path / "secret-state-name.sqlite3"
    with StateStore(state_path, environment="local"):
        result = subprocess.run(
            [
                "epub-news-feeder",
                "generate",
                "--config",
                str(config),
                "--state",
                str(state_path),
                "--output",
                str(tmp_path / "output"),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    assert result.returncode == 3
    assert "code=GENERATION_FAILED" in result.stderr
    assert "secret-state-name" not in result.stderr
    assert "Traceback" not in result.stderr
