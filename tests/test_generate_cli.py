from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from zipfile import ZipFile

import pytest
from lxml import etree


class EditionFixtureHandler(BaseHTTPRequestHandler):
    body = " ".join(f"complete-journalism-{index}" for index in range(180))
    hits: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        type(self).hits.append(self.path)
        if self.path == "/robots.txt":
            content_type = "text/plain"
            payload = b"User-agent: *\nAllow: /\n"
            status = 200
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
            content_type = "text/plain"
            payload = b"not found"
            status = 404
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.mark.acceptance
@pytest.mark.epubcheck
def test_ticket_13_cli_generates_valid_body_free_local_edition(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), EditionFixtureHandler)
    EditionFixtureHandler.hits = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = tmp_path / "publication.yaml"
        config.write_text(
            f"""
version: 1
sources:
  fixture:
    title: Fixture News
    publisher_id: fixture-publisher
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
    budget:
      max_articles: 2
      min_articles: 1
    sections:
      - id: world
        title: World
        sources: [fixture]
""".lstrip(),
            encoding="utf-8",
        )
        state = tmp_path / "state.sqlite3"
        output = tmp_path / "editions"
        diagnostics = tmp_path / "diagnostics"
        result = subprocess.run(
            [
                "epub-news-feeder",
                "generate",
                "--config",
                str(config),
                "--state",
                str(state),
                "--output",
                str(output),
                "--diagnostics",
                str(diagnostics),
                "--run-id",
                "20260809T060000Z-AAAAAAAA",
                "--at",
                "2026-08-09T06:00:00Z",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert result.returncode == 0, result.stderr
    assert "code=EDITION_DELIVERED" in result.stdout
    assert "articles=1" in result.stdout
    assert result.stderr == ""
    editions = list(output.glob("*.epub"))
    assert len(editions) == 1
    epub_path = editions[0]

    with ZipFile(epub_path) as archive:
        section_path = next(name for name in archive.namelist() if name.startswith("OEBPS/world-"))
        section = etree.fromstring(archive.read(section_path))
        rendered = " ".join(text for text in section.itertext() if isinstance(text, str))
    assert EditionFixtureHandler.body in rendered
    assert "This preview is discovery metadata only" not in rendered
    assert "A. Reporter" in rendered
    assert "Fixture News" in rendered
    assert "https://publisher.example/reports/complete" in etree.tostring(
        section, encoding="unicode"
    )
    assert "20260809T060000Z-AAAAAAAA" in rendered

    database_bytes = state.read_bytes()
    diagnostic_bytes = next(diagnostics.glob("*.jsonl")).read_bytes()
    assert b"complete-journalism-42" not in database_bytes
    assert b"complete-journalism-42" not in diagnostic_bytes
    with sqlite3.connect(state) as connection:
        assert connection.execute("SELECT status FROM runs").fetchone() == ("delivered",)
        delivered_ids = {
            row[0] for row in connection.execute("SELECT article_id FROM deliveries").fetchall()
        }
    events = [json.loads(line) for line in diagnostic_bytes.splitlines()]
    assert any(event.get("evidence_id") == "fixture-20260809" for event in events)
    assert {
        event["article_id"] for event in events if event["code"] == "ARTICLE_SELECTED"
    } == delivered_ids

    validation = subprocess.run(
        [
            "java",
            "-jar",
            ".local/tools/epubcheck-5.3.0/epubcheck.jar",
            "--failonwarnings",
            str(epub_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert validation.returncode == 0, validation.stdout + validation.stderr
