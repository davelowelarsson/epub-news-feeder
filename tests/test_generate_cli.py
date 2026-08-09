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
from epub_news_feeder.application import GenerationError, RetryableGenerationError, generate_edition
from epub_news_feeder.config import load_config
from epub_news_feeder.delivery import DeliveryReceipt
from epub_news_feeder.editorial import ArticleEvidence, CallUsage, StructuredCall
from epub_news_feeder.state import brief_id as compute_brief_id


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
<item><title>An unrelated bulletin</title><link>{origin}/brief/unrelated</link>
<guid>brief-unrelated</guid><pubDate>Sat, 08 Aug 2026 06:00:00 GMT</pubDate></item>
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
    # Every eligible Brief arrives: they are capped apart from the Article Budget and no
    # longer compete with journalism for an Article Slot.
    assert "articles=1" in result.stdout
    assert "briefs=3" in result.stdout
    assert "read_items=4" in result.stdout
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
    assert "In Brief" in xhtml
    # The chrome the aggregated chapter exists to remove is gone.
    assert "Read report at publisher" not in xhtml
    assert "[Publisher link]" not in xhtml
    assert "this Edition does not reproduce the article text" not in xhtml
    # Briefs carry no relevance ranking, so every unmuted headline within the cap arrives,
    # ordered newest first across Sources.
    for headline in (
        "New report about the harbour boat",
        "Older report about the harbour boat",
        "An unrelated bulletin",
    ):
        assert headline in xhtml, headline
    assert xhtml.index("New report about the harbour boat") < xhtml.index(
        "Older report about the harbour boat"
    )
    assert xhtml.index("Older report about the harbour boat") < xhtml.index("An unrelated bulletin")
    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM articles").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM deliveries").fetchone() == (1,)
        stored_urls = connection.execute("SELECT canonical_url FROM articles").fetchall()
        assert stored_urls == [("https://publisher.example/harbour",)]
    state_bytes = (tmp_path / "state.sqlite3").read_bytes()
    assert b"New report about the harbour boat" not in state_bytes
    assert b"/brief/new" not in state_bytes
    assert b"An unrelated bulletin" not in state_bytes
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
    assert "articles=1 briefs=3 read_items=4" in repeated.stdout


class BriefSuppressionFixtureHandler(BaseHTTPRequestHandler):
    """Serves a Brief feed whose content changes across requests to model two Editions.

    ``phase`` is mutated by the test between subprocess invocations of the CLI: the server
    itself lives in the test process and stays up across every ``generate`` call.
    """

    phase: ClassVar[str] = "day-one"

    def do_GET(self) -> None:
        if self.path == "/robots.txt":
            content_type = "text/plain"
            payload = b"User-agent: *\nAllow: /\n"
            status = 200
        elif self.path == "/briefs.xml":
            content_type = "application/rss+xml"
            server = cast(ThreadingHTTPServer, self.server)
            origin = f"http://127.0.0.1:{server.server_port}"
            if type(self).phase == "day-one":
                items = f"""
<item><title>Harbour spill under investigation</title>
<link>{origin}/brief/spill</link>
<guid>brief-spill-day-one</guid><pubDate>Sun, 09 Aug 2026 06:00:00 GMT</pubDate></item>
"""
            else:
                items = f"""
<item><title>Harbour spill investigation continues</title>
<link>{origin}/brief/spill</link>
<guid>brief-spill-day-two</guid><pubDate>Mon, 10 Aug 2026 06:00:00 GMT</pubDate></item>
<item><title>New coastal report emerges</title>
<link>{origin}/brief/new-report</link>
<guid>brief-new-day-two</guid><pubDate>Mon, 10 Aug 2026 06:05:00 GMT</pubDate></item>
"""
            payload = f"""<rss version="2.0"><channel><title>Ekot Fixture</title>{items}
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
def test_brief_delivery_suppresses_repeats_but_not_new_urls_or_other_publications(
    tmp_path: Path,
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), BriefSuppressionFixtureHandler)
    BriefSuppressionFixtureHandler.phase = "day-one"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = tmp_path / "publication.yaml"
    config.write_text(
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
  - id: weekend
    title: Weekend Edition
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
    state = tmp_path / "state.sqlite3"
    output = tmp_path / "editions"

    def run(*, publication: str, run_id: str, at: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "epub-news-feeder",
                "generate",
                "--config",
                str(config),
                "--state",
                str(state),
                "--output",
                str(output),
                "--publication",
                publication,
                "--run-id",
                run_id,
                "--at",
                at,
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    try:
        day_one = run(
            publication="daily",
            run_id="20260809T060000Z-DAYONEAA",
            at="2026-08-09T06:00:00Z",
        )
        assert day_one.returncode == 0, day_one.stderr
        assert "briefs=1" in day_one.stdout
        with ZipFile(next(output.glob("*DAYONEAA*.epub"))) as archive:
            xhtml = " ".join(
                archive.read(name).decode()
                for name in archive.namelist()
                if name.endswith(".xhtml")
            )
        assert "Harbour spill under investigation" in xhtml

        BriefSuppressionFixtureHandler.phase = "day-two"
        day_two = run(
            publication="daily",
            run_id="20260810T060000Z-DAYTWOAA",
            at="2026-08-10T06:00:00Z",
        )
        assert day_two.returncode == 0, day_two.stderr
        # The spill is the same report under a reworded headline: it stays suppressed. Only
        # the genuinely new canonical URL is delivered.
        assert "briefs=1" in day_two.stdout
        with ZipFile(next(output.glob("*DAYTWOAA*.epub"))) as archive:
            xhtml = " ".join(
                archive.read(name).decode()
                for name in archive.namelist()
                if name.endswith(".xhtml")
            )
        assert "New coastal report emerges" in xhtml
        assert "Harbour spill under investigation" not in xhtml
        assert "Harbour spill investigation continues" not in xhtml

        BriefSuppressionFixtureHandler.phase = "day-one"
        weekend = run(
            publication="weekend",
            run_id="20260809T060000Z-WEEKENDA",
            at="2026-08-09T06:00:00Z",
        )
        assert weekend.returncode == 0, weekend.stderr
        # Suppression is scoped per Publication: "daily" having delivered the spill report
        # does not suppress it for "weekend".
        assert "briefs=1" in weekend.stdout
        with ZipFile(next(output.glob("*WEEKENDA*.epub"))) as archive:
            xhtml = " ".join(
                archive.read(name).decode()
                for name in archive.namelist()
                if name.endswith(".xhtml")
            )
        assert "Harbour spill under investigation" in xhtml
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    with sqlite3.connect(state) as connection:
        counts = dict(
            connection.execute(
                "SELECT publication_id, COUNT(*) FROM brief_deliveries GROUP BY publication_id"
            ).fetchall()
        )
        # "daily" delivered the spill report on day one and the new report on day two;
        # "weekend" independently delivered the spill report once.
        assert counts == {"daily": 2, "weekend": 1}
        assert connection.execute("SELECT COUNT(*) FROM articles").fetchone() == (0,)


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

        def drain_usage(self) -> tuple[CallUsage, ...]:
            return (
                CallUsage(
                    role="editorial",
                    model="gemma4:12b-mlx",
                    total_duration_ms=4500,
                    load_duration_ms=500,
                    input_tokens=1200,
                    output_tokens=90,
                ),
            )

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
    assert "generated on this device" in xhtml
    assert "remote provider" not in xhtml
    assert "The harbour investigation is continuing." in xhtml
    assert "https://publisher.example/harbour" in xhtml
    diagnostics = (tmp_path / "diagnostics" / "20260809T081000Z-DDDDDDDD.jsonl").read_text()
    assert "EDITORIAL_ACCEPTED" in diagnostics
    assert "The harbour investigation is continuing" not in diagnostics
    measured = [
        json.loads(line)
        for line in diagnostics.splitlines()
        if json.loads(line)["code"] == "EDITORIAL_MEASURED"
    ]
    assert measured, "the editorial path must record a body-free measurement"
    assert measured[0]["input_tokens"] == 1200
    assert measured[0]["output_tokens"] == 90
    assert measured[0]["input_characters"] > 0
    assert measured[0]["duration_ms"] >= 0


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


@pytest.mark.acceptance
@pytest.mark.epubcheck
def test_brief_delivery_recorded_correctly_through_spooled_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MixedEditionFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config_path = tmp_path / "publication.yaml"
    config_path.write_text(
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
    configuration = load_config(config_path)
    state = tmp_path / "state.sqlite3"
    output = tmp_path / "editions"
    diagnostics = tmp_path / "diagnostics"
    run_id = "20260809T080000Z-RESUMEAAA"
    generated_at = datetime(2026, 8, 9, 8, tzinfo=UTC)
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

        # A validated-but-undelivered Run must suppress nothing yet: the crash happened before
        # finalization, so no Brief has been recorded.
        with sqlite3.connect(state) as connection:
            assert connection.execute("SELECT COUNT(*) FROM brief_deliveries").fetchone() == (0,)

        monkeypatch.setattr(application, "deliver_local", original_delivery)
        result = generate_edition(
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

    assert result.brief_count == 3
    # The spooled-resume path (not the normal in-process path) is what finalized this Run, so
    # the identities recorded prove recording happens correctly there too.
    expected_ids = {
        compute_brief_id(f"http://127.0.0.1:{server.server_port}/brief/{slug}")
        for slug in ("new", "old", "unrelated")
    }
    with sqlite3.connect(state) as connection:
        recorded_ids = {
            str(row[0])
            for row in connection.execute(
                "SELECT brief_id FROM brief_deliveries WHERE publication_id = 'daily'"
            )
        }
    assert recorded_ids == expected_ids


class OneBriefFixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/robots.txt":
            content_type = "text/plain"
            payload = b"User-agent: *\nAllow: /\n"
            status = 200
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
def test_brief_suppression_ignores_an_abandoned_run(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), OneBriefFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def config(*, min_articles: int) -> str:
        return f"""
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
    budget: {{max_articles: 6, min_articles: {min_articles}}}
    max_briefs: 6
    sections:
      - id: current
        title: Current reporting
        sources: [briefs]
""".lstrip()

    state = tmp_path / "state.sqlite3"
    output = tmp_path / "editions"
    diagnostics = tmp_path / "diagnostics"
    generated_at = datetime(2026, 8, 9, 6, tzinfo=UTC)
    try:
        # A Brief alone never meets an Article minimum, so this Run is abandoned outright —
        # never validated, never delivered.
        unmet_path = tmp_path / "unmet.yaml"
        unmet_path.write_text(config(min_articles=1), encoding="utf-8")
        unmet_config = load_config(unmet_path)
        with pytest.raises(GenerationError, match="did not meet the publication minimum"):
            generate_edition(
                unmet_config,
                state_path=state,
                output_directory=output,
                diagnostics_directory=diagnostics,
                run_id="20260809T060000Z-ABANDONAA",
                generated_at=generated_at,
            )
        with sqlite3.connect(state) as connection:
            assert connection.execute("SELECT COUNT(*) FROM brief_deliveries").fetchone() == (0,)
            assert connection.execute("SELECT status FROM runs").fetchone() == ("failed",)

        # The same Brief, in a Run that can succeed, is not suppressed by the abandoned attempt.
        met_path = tmp_path / "met.yaml"
        met_path.write_text(config(min_articles=0), encoding="utf-8")
        met_config = load_config(met_path)
        result = generate_edition(
            met_config,
            state_path=state,
            output_directory=output,
            diagnostics_directory=diagnostics,
            run_id="20260809T060000Z-SUCCEEDAA",
            generated_at=generated_at,
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert result.brief_count == 1


class RemoteEditorialFixtureHandler(BaseHTTPRequestHandler):
    """Two full-text publishers whose bodies are distinguishable by a single token."""

    granted_body = " ".join(f"granted-token-{index}" for index in range(180))
    withheld_body = " ".join(f"withheld-token-{index}" for index in range(180))

    def do_GET(self) -> None:
        if self.path == "/robots.txt":
            content_type, status = "text/plain", 200
            payload = b"User-agent: *\nAllow: /\n"
        elif self.path in {"/granted.xml", "/withheld.xml"}:
            granted = self.path == "/granted.xml"
            body = type(self).granted_body if granted else type(self).withheld_body
            name = "Granted Publisher" if granted else "Withheld Publisher"
            slug = "granted" if granted else "withheld"
            content_type, status = "application/rss+xml", 200
            payload = f"""<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>{name}</title><item>
<title>A {slug} report</title>
<link>https://{slug}.example/report</link><guid>{slug}-report</guid>
<author>Jamie Reporter</author><pubDate>Sat, 08 Aug 2026 08:00:00 GMT</pubDate>
<content:encoded><![CDATA[<p>{body}</p>]]></content:encoded>
</item></channel></rss>""".encode()
        else:
            content_type, status = "text/plain", 404
            payload = b"not found"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def _remote_editorial_config(port: int) -> str:
    return f"""
version: 1
remote_providers:
  openai:
    training_opt_in: false
    store: false
    application_state_retention_days: 0
    max_abuse_retention_days: 30
    tools: none
sources:
  granted:
    title: Granted Publisher
    default_article_language: en
    publisher_id: granted.example
    allowed_publisher_origins: [https://granted.example]
    feed_url: http://127.0.0.1:{port}/granted.xml
    acquisition: feed
    llm_processing: remote_allowed
    rights:
      basis: rightsholder_granted_fixture
      audience: single_operator
      attribution_required: true
      media_reuse: false
    eligibility:
      evidence_reviewed_at: 2026-08-09
      review_expires_at: 2026-09-08
      evidence_id: granted-fixture
      feed_acquisition: allow
      page_acquisition: allow
      retention: allow
      private_distribution: allow
      local_llm: allow
      remote_llm: allow
  withheld:
    title: Withheld Publisher
    default_article_language: en
    publisher_id: withheld.example
    allowed_publisher_origins: [https://withheld.example]
    feed_url: http://127.0.0.1:{port}/withheld.xml
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
      evidence_id: withheld-fixture
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
    budget: {{max_articles: 2, min_articles: 1}}
    editorial:
      enabled: true
      remote_processing: true
      provider: openai
      model_pair:
        editorial_model: gpt-5.4-2026-03-05
        verifier_model: gpt-5.4-mini-2026-03-17
        editorial_prompt_version: article-summary-v1
        verifier_prompt_version: evidence-check-v1
        schema_version: 1
      capabilities: [article_summary]
      cost_envelope: {{max_calls: 8, max_tokens: 12000}}
    sections:
      - id: current
        title: Current reporting
        sources: [granted, withheld]
""".lstrip()


@pytest.mark.security
@pytest.mark.epubcheck
def test_editorial_preflight_sends_only_the_source_that_granted_remote_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remote route reads `remote_llm`, and a Source that permits only local processing
    never leaves the machine.

    Both publishers here permit a local model. Only one permits a remote one. The difference
    has to be visible in what crosses the provider boundary, not merely in a policy field, so
    this asserts on the calls themselves: the withheld publisher's body token must appear
    nowhere in any request, and the reader must be told which Source was left out.
    """

    server = ThreadingHTTPServer(("127.0.0.1", 0), RemoteEditorialFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = tmp_path / "publication.yaml"
    config.write_text(_remote_editorial_config(server.server_port), encoding="utf-8")
    seen: list[StructuredCall] = []

    class StubProvider:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def drain_usage(self) -> tuple[CallUsage, ...]:
            return (
                CallUsage(
                    role="editorial",
                    model="gpt-5.4-2026-03-05",
                    total_duration_ms=0,
                    load_duration_ms=0,
                    input_tokens=800,
                    output_tokens=60,
                ),
            )

        def complete(self, call: StructuredCall) -> object:
            seen.append(call)
            if call.role == "editorial":
                articles = cast(list[dict[str, object]], call.input["articles"])
                article_id = str(articles[0]["article_id"])
                return {
                    "summaries": [
                        {
                            "article_id": article_id,
                            "sentences": [
                                {
                                    "text": "The granted report sets out what happened next.",
                                    "citations": [article_id],
                                }
                            ],
                        }
                    ]
                }
            return {"findings": [{"summary_index": 0, "sentence_index": 0, "status": "supported"}]}

    monkeypatch.setattr(application, "OpenAIResponsesProvider", StubProvider)
    monkeypatch.setattr(
        application,
        "OllamaStructuredProvider",
        _unreachable_provider("the remote route must never fall back to the local provider"),
    )
    try:
        result = generate_edition(
            load_config(config),
            state_path=tmp_path / "state.sqlite3",
            output_directory=tmp_path / "editions",
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260809T081000Z-REMOTEAA",
            generated_at=datetime(2026, 8, 9, 8, 10, tzinfo=UTC),
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert result.article_count == 2, "both Articles belong in the Edition either way"
    assert seen, "the remote provider must have been called"
    sent = json.dumps([call.model_dump(mode="json") for call in seen])
    assert "granted-token-0" in sent
    assert "withheld-token-0" not in sent
    assert "Withheld Publisher" not in sent

    with ZipFile(result.receipt.path) as archive:
        xhtml = " ".join(
            archive.read(name).decode() for name in archive.namelist() if name.endswith(".xhtml")
        )
    assert "The granted report sets out what happened next." in xhtml
    # The reader is told which Source was excluded, and it is the route actually taken that
    # decides: a publisher permitting local but not remote processing is named here.
    assert "Withheld Publisher" in xhtml
    # And the method note states the route honestly. A remote Edition claiming its summaries
    # were produced on the device would be the most misleading sentence in the book.
    assert "generated by a remote provider" in xhtml
    assert "generated on this device" not in xhtml

    diagnostics = [
        json.loads(line)
        for line in (tmp_path / "diagnostics" / "20260809T081000Z-REMOTEAA.jsonl")
        .read_text()
        .splitlines()
    ]
    route = next(event for event in diagnostics if event["code"] == "EDITORIAL_ROUTE")
    assert route["route"] == "remote"
    assert route["provider"] == "openai"


def _unreachable_provider(message: str) -> type:
    class Unreachable:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError(message)

    return Unreachable
