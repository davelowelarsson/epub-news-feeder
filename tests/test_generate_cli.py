from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar, cast
from zipfile import ZipFile

import pytest
from lxml import etree

from epub_news_feeder import application
from epub_news_feeder.application import RetryableGenerationError, generate_edition
from epub_news_feeder.config import load_config
from epub_news_feeder.delivery import DeliveryReceipt
from epub_news_feeder.editorial import ArticleEvidence, StructuredCall


def test_editorial_evidence_is_batched_by_article_language() -> None:
    def evidence(article_id: str, language: str) -> ArticleEvidence:
        return ArticleEvidence(
            article_id=article_id,
            title="Title",
            publisher="Publisher",
            canonical_url=f"https://example.test/{article_id}",
            published_at="2026-08-09",
            language=language,
            lead_passage="Lead passage with enough context.",
            body="Body text with enough context for a summary.",
        )

    batches = application._editorial_batches(
        (
            evidence("sv-1", "sv"),
            evidence("en-1", "en"),
            evidence("sv-2", "sv-SE"),
            evidence("en-2", "en"),
            evidence("en-3", "en"),
        )
    )

    assert [[item.article_id for item in batch] for batch in batches] == [
        ["en-1"],
        ["en-2"],
        ["en-3"],
        ["sv-1"],
        ["sv-2"],
    ]


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


class MixedEditionFixtureHandler(BaseHTTPRequestHandler):
    body = " ".join(f"verified-report-{index}" for index in range(180))

    def do_GET(self) -> None:
        if self.path == "/robots.txt":
            content_type = "text/plain"
            payload = b"User-agent: *\nAllow: /\n"
            status = 200
        elif self.path == "/full.xml":
            content_type = "application/rss+xml"
            payload = f"""<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Full Publisher</title><item>
<title>Harbour investigation continues</title>
<link>https://publisher.example/harbour</link><guid>full-harbour</guid>
<author>Jamie Reporter</author><pubDate>Sat, 08 Aug 2026 08:00:00 GMT</pubDate>
<content:encoded><![CDATA[<p>{type(self).body}</p>]]></content:encoded>
</item></channel></rss>""".encode()
            status = 200
        elif self.path == "/briefs.xml":
            content_type = "application/rss+xml"
            server = cast(ThreadingHTTPServer, self.server)
            origin = f"http://127.0.0.1:{server.server_port}"
            payload = f"""<rss version="2.0"><channel><title>Ekot Fixture</title>
<item><title>New report about the harbour boat</title><link>{origin}/brief/new</link>
<guid>brief-new</guid><pubDate>Sat, 08 Aug 2026 09:00:00 GMT</pubDate></item>
<item><title>Older report about the harbour boat</title><link>{origin}/brief/old</link>
<guid>brief-old</guid><pubDate>Sat, 08 Aug 2026 07:00:00 GMT</pubDate></item>
<item><title>Unselected unrelated bulletin</title><link>{origin}/brief/unselected</link>
<guid>brief-unselected</guid><pubDate>Sat, 08 Aug 2026 06:00:00 GMT</pubDate></item>
</channel></rss>""".encode()
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
def test_ticket_02_ticket_11_ticket_13_cli_generates_valid_body_free_edition(
    tmp_path: Path,
) -> None:
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
    assert output.stat().st_mode & 0o077 == 0
    assert epub_path.stat().st_mode & 0o077 == 0

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


@pytest.mark.acceptance
@pytest.mark.epubcheck
def test_link_only_reporting_is_selected_into_the_finite_reading_flow(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MixedEditionFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = tmp_path / "publication.yaml"
        config.write_text(
            f"""
version: 1
sources:
  full:
    title: Full Publisher
    default_article_language: en
    publisher_id: publisher.example
    feed_url: http://127.0.0.1:{server.server_port}/full.xml
    acquisition: feed
    llm_processing: local_only
    rights:
      basis: fixture
      audience: single_operator
      attribution_required: true
      media_reuse: false
    eligibility:
      evidence_reviewed_at: 2026-08-09
      review_expires_at: 2026-09-08
      evidence_id: full-fixture
      feed_acquisition: allow
      page_acquisition: allow
      retention: allow
      private_distribution: allow
      local_llm: allow
      remote_llm: deny
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
    budget: {{max_articles: 2, min_articles: 1}}
    sections:
      - id: current
        title: Current reporting
        sources: [full, briefs]
""".lstrip(),
            encoding="utf-8",
        )
        output = tmp_path / "editions"
        result = subprocess.run(
            [
                "epub-news-feeder",
                "generate",
                "--config",
                str(config),
                "--state",
                str(tmp_path / "state.sqlite3"),
                "--output",
                str(output),
                "--run-id",
                "20260809T080000Z-CCCCCCCC",
                "--at",
                "2026-08-09T08:00:00Z",
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
    assert "articles=1" in result.stdout
    assert "publisher_links=1" in result.stdout
    assert "read_items=2" in result.stdout
    with ZipFile(next(output.glob("*.epub"))) as archive:
        xhtml = " ".join(
            archive.read(name).decode() for name in archive.namelist() if name.endswith(".xhtml")
        )
    assert "Harbour investigation continues" in xhtml
    assert "Jamie Reporter" in xhtml
    assert "2026-08-08" in xhtml
    assert "New report about the harbour boat" in xhtml
    assert "Sveriges Radio Ekot" in xhtml
    assert "2026-08-08" in xhtml
    assert "Read report at publisher" in xhtml
    assert "Older report about the harbour boat" not in xhtml
    assert "Unselected unrelated bulletin" not in xhtml
    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM articles").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM deliveries").fetchone() == (1,)
        stored_urls = connection.execute("SELECT canonical_url FROM articles").fetchall()
        assert stored_urls == [("https://publisher.example/harbour",)]
    state_bytes = (tmp_path / "state.sqlite3").read_bytes()
    assert b"New report about the harbour boat" not in state_bytes
    assert b"/brief/new" not in state_bytes
    diagnostic_text = (tmp_path / "diagnostics" / "20260809T080000Z-CCCCCCCC.jsonl").read_text()
    assert "New report about the harbour boat" not in diagnostic_text
    assert "/brief/new" not in diagnostic_text

    repeated = subprocess.run(
        [
            "epub-news-feeder",
            "generate",
            "--config",
            str(config),
            "--state",
            str(tmp_path / "state.sqlite3"),
            "--output",
            str(output),
            "--run-id",
            "20260809T080000Z-CCCCCCCC",
            "--at",
            "2026-08-09T08:00:00Z",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert repeated.returncode == 0, repeated.stderr
    assert "articles=1 publisher_links=1 read_items=2" in repeated.stdout


@pytest.mark.acceptance
@pytest.mark.epubcheck
def test_local_editorial_summary_is_verified_and_rendered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MixedEditionFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = tmp_path / "publication.yaml"
    config.write_text(
        f"""
version: 1
sources:
  full:
    title: Full Publisher
    default_article_language: en
    publisher_id: publisher.example
    feed_url: http://127.0.0.1:{server.server_port}/full.xml
    acquisition: feed
    llm_processing: local_only
    rights:
      basis: fixture
      audience: single_operator
      attribution_required: true
      media_reuse: false
    eligibility:
      evidence_reviewed_at: 2026-08-09
      review_expires_at: 2026-09-08
      evidence_id: full-fixture
      feed_acquisition: allow
      page_acquisition: allow
      retention: allow
      private_distribution: allow
      local_llm: allow
      remote_llm: deny
publications:
  - id: daily
    title: Daily Edition
    language: en
    budget: {{max_articles: 1, min_articles: 1}}
    editorial:
      enabled: true
      provider: ollama
      model_pair:
        editorial_model: gemma4:12b-mlx
        verifier_model: gemma4:e4b-mlx
        editorial_prompt_version: editorial-v1
        verifier_prompt_version: verifier-v1
        schema_version: 1
      capabilities: [article_summary]
      cost_envelope: {{max_calls: 4, max_tokens: 12000}}
      ollama_host: http://127.0.0.1:11434
    sections:
      - id: current
        title: Current reporting
        sources: [full]
""".lstrip(),
        encoding="utf-8",
    )

    class StubProvider:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def complete(self, call: StructuredCall) -> object:
            if call.role == "editorial":
                articles = cast(list[dict[str, object]], call.input["articles"])
                assert articles[0]["language"] == "en"
                article_id = str(articles[0]["article_id"])
                return {
                    "summaries": [
                        {
                            "article_id": article_id,
                            "sentences": [
                                {
                                    "text": "The harbour investigation is continuing.",
                                    "citations": [article_id],
                                }
                            ],
                        }
                    ]
                }
            return {"findings": [{"summary_index": 0, "sentence_index": 0, "status": "supported"}]}

    monkeypatch.setattr(application, "OllamaStructuredProvider", StubProvider)
    try:
        result = generate_edition(
            load_config(config),
            state_path=tmp_path / "state.sqlite3",
            output_directory=tmp_path / "editions",
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260809T081000Z-DDDDDDDD",
            generated_at=datetime(2026, 8, 9, 8, 10, tzinfo=UTC),
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    with ZipFile(result.receipt.path) as archive:
        xhtml = " ".join(
            archive.read(name).decode() for name in archive.namelist() if name.endswith(".xhtml")
        )
    assert "AI-generated summary" in xhtml
    assert "The harbour investigation is continuing." in xhtml
    assert "https://publisher.example/harbour" in xhtml
    diagnostics = (tmp_path / "diagnostics" / "20260809T081000Z-DDDDDDDD.jsonl").read_text()
    assert "EDITORIAL_ACCEPTED" in diagnostics
    assert "The harbour investigation is continuing" not in diagnostics


@pytest.mark.acceptance
@pytest.mark.epubcheck
def test_validated_spool_resumes_without_reacquiring_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), EditionFixtureHandler)
    EditionFixtureHandler.hits = []
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
    state = tmp_path / "state.sqlite3"
    output = tmp_path / "editions"
    diagnostics = tmp_path / "diagnostics"
    run_id = "20260809T070000Z-BBBBBBBB"
    generated_at = datetime(2026, 8, 9, 7, tzinfo=UTC)
    original_delivery = application.deliver_local  # type: ignore[attr-defined]

    def fail_final_delivery(
        epub_bytes: bytes, *, output_directory: Path, filename: str
    ) -> DeliveryReceipt:
        if output_directory == output:
            raise OSError("simulated unavailable final target")
        return original_delivery(epub_bytes, output_directory=output_directory, filename=filename)

    monkeypatch.setattr(application, "deliver_local", fail_final_delivery)
    try:
        with pytest.raises(RetryableGenerationError, match="Delivery remains pending"):
            generate_edition(
                configuration,
                state_path=state,
                output_directory=output,
                diagnostics_directory=diagnostics,
                run_id=run_id,
                generated_at=generated_at,
            )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    hits_before_resume = list(EditionFixtureHandler.hits)
    monkeypatch.setattr(application, "deliver_local", original_delivery)
    result = generate_edition(
        configuration,
        state_path=state,
        output_directory=output,
        diagnostics_directory=diagnostics,
        run_id=run_id,
        generated_at=generated_at,
    )

    assert result.article_count == 1
    assert result.receipt.path.is_file()
    assert EditionFixtureHandler.hits == hits_before_resume
    assert not (tmp_path / "pending-editions" / f"{run_id}.epub").exists()
    with sqlite3.connect(state) as connection:
        assert connection.execute("SELECT status FROM runs").fetchone() == ("delivered",)
        assert connection.execute("SELECT COUNT(*) FROM pending_deliveries").fetchone() == (0,)

    repeated = generate_edition(
        configuration,
        state_path=state,
        output_directory=output,
        diagnostics_directory=diagnostics,
        run_id=run_id,
        generated_at=generated_at,
    )
    assert repeated.receipt == result.receipt
    assert EditionFixtureHandler.hits == hits_before_resume
