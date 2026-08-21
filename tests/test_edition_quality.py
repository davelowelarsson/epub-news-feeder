"""Reader-facing quality rules observed failing in delivered Editions (Aug 10-21, 2026)."""

from __future__ import annotations

import json
import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from epub_news_feeder.application import generate_edition
from epub_news_feeder.config import load_config
from epub_news_feeder.models import Configuration

_EVIDENCE = """
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
"""


@contextmanager
def _mutable_feed_server(holder: dict[str, str]) -> Iterator[ThreadingHTTPServer]:
    """Serve ``holder["xml"]`` as the feed, so one test can change it between runs."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/robots.txt":
                payload, status = b"User-agent: *\nAllow: /\n", 200
            elif self.path == "/feed.xml":
                payload, status = holder["xml"].encode(), 200
            else:
                payload, status = b"not found", 404
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@contextmanager
def _feed_server(feed_xml: str) -> Iterator[ThreadingHTTPServer]:
    with _mutable_feed_server({"xml": feed_xml}) as server:
        yield server


def _configuration(tmp_path: Path, yaml_text: str) -> Configuration:
    config_path = tmp_path / "publication.yaml"
    config_path.write_text(yaml_text, encoding="utf-8")
    return load_config(config_path)


def _feed(items: str) -> str:
    return (
        '<?xml version="1.0"?>'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>Fixture News</title>{items}</channel></rss>"
    )


def _item(*, title: str, slug: str, body_words: list[str], pub_date: str) -> str:
    body = " ".join(body_words) + "."
    return (
        f"<item><title>{title}</title>"
        f"<link>https://publisher.example/reports/{slug}</link>"
        f"<guid>{slug}</guid><author>A. Reporter</author>"
        f"<pubDate>{pub_date}</pubDate>"
        f"<content:encoded><![CDATA[<p>{body}</p>]]></content:encoded></item>"
    )


def _epub_text(epub_path: Path) -> str:
    with zipfile.ZipFile(epub_path) as archive:
        return "".join(
            archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if name.endswith(".xhtml")
        )


def _diagnostic_codes(diagnostics_directory: Path) -> set[str]:
    codes: set[str] = set()
    for path in diagnostics_directory.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            codes.add(str(json.loads(line)["code"]))
    return codes


@pytest.mark.acceptance
@pytest.mark.epubcheck
def test_one_headline_at_two_urls_is_one_article_in_the_edition(tmp_path: Path) -> None:
    """Observed live: a publisher re-published one piece at a second URL under another
    byline, and the same headline filled two slots of one Section on one morning."""

    title = "Fyra av tio elever saknar behörighet"
    feed_xml = _feed(
        _item(
            title=title,
            slug="behorighet",
            body_words=[f"shorter-{index}" for index in range(90)],
            pub_date="Wed, 19 Aug 2026 06:00:00 GMT",
        )
        + _item(
            title=title,
            slug="behorighet-2",
            body_words=[f"longer-{index}" for index in range(130)],
            pub_date="Wed, 19 Aug 2026 07:00:00 GMT",
        )
    )
    with _feed_server(feed_xml) as server:
        configuration = _configuration(
            tmp_path,
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
{_EVIDENCE}
publications:
  - id: morning
    title: Morning Briefing
    language: en
    budget: {{max_articles: 3, min_articles: 1}}
    sections:
      - id: world
        title: World
        sources: [fixture]
""".lstrip(),
        )
        result = generate_edition(
            configuration,
            state_path=tmp_path / "state.sqlite3",
            output_directory=tmp_path / "editions",
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260820T040000Z-TWINAAAA",
            generated_at=datetime(2026, 8, 20, 4, tzinfo=UTC),
        )

    assert result.article_count == 1
    rendered = _epub_text(result.receipt.path)
    assert "longer-5" in rendered, "the fuller body must be the survivor"
    assert "shorter-5" not in rendered
    assert "ARTICLE_TITLE_TWIN_SUPPRESSED" in _diagnostic_codes(tmp_path / "diagnostics")


@pytest.mark.acceptance
@pytest.mark.epubcheck
def test_a_recurring_column_reusing_its_title_is_not_a_twin(tmp_path: Path) -> None:
    """A weekly column publishes under the same headline every time. Two same-title
    articles a week apart are two columns, not one story at two URLs."""

    title = "Veckans pass"
    feed_xml = _feed(
        _item(
            title=title,
            slug="veckans-pass-33",
            body_words=[f"week-one-{index}" for index in range(90)],
            pub_date="Mon, 10 Aug 2026 06:00:00 GMT",
        )
        + _item(
            title=title,
            slug="veckans-pass-34",
            body_words=[f"week-two-{index}" for index in range(90)],
            pub_date="Mon, 17 Aug 2026 06:00:00 GMT",
        )
    )
    with _feed_server(feed_xml) as server:
        configuration = _configuration(
            tmp_path,
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
{_EVIDENCE}
publications:
  - id: morning
    title: Morning Briefing
    language: en
    budget: {{max_articles: 3, min_articles: 1}}
    sections:
      - id: world
        title: World
        sources: [fixture]
""".lstrip(),
        )
        result = generate_edition(
            configuration,
            state_path=tmp_path / "state.sqlite3",
            output_directory=tmp_path / "editions",
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260818T040000Z-COLUMNAA",
            generated_at=datetime(2026, 8, 18, 4, tzinfo=UTC),
        )

    assert result.article_count == 2


@pytest.mark.acceptance
@pytest.mark.epubcheck
def test_a_story_republished_at_a_new_url_does_not_return_the_next_morning(
    tmp_path: Path,
) -> None:
    """The within-run twin suppression cannot see yesterday: the delivered URL is no longer
    a candidate, so the republished URL arrives as a singleton. A recently delivered
    headline from the same publisher under a different identity is the same story."""

    title = "Fyra av tio elever saknar behörighet"
    holder = {
        "xml": _feed(
            _item(
                title=title,
                slug="behorighet",
                body_words=[f"first-{index}" for index in range(90)],
                pub_date="Wed, 19 Aug 2026 06:00:00 GMT",
            )
        )
    }
    with _mutable_feed_server(holder) as server:
        configuration = _configuration(
            tmp_path,
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
{_EVIDENCE}
publications:
  - id: morning
    title: Morning Briefing
    language: en
    budget: {{max_articles: 3, min_articles: 1}}
    sections:
      - id: world
        title: World
        sources: [fixture]
""".lstrip(),
        )
        generate_edition(
            configuration,
            state_path=tmp_path / "state.sqlite3",
            output_directory=tmp_path / "editions",
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260819T040000Z-REPUBAAA",
            generated_at=datetime(2026, 8, 19, 4, tzinfo=UTC),
        )

        holder["xml"] = _feed(
            _item(
                title=title,
                slug="behorighet-2",
                body_words=[f"second-{index}" for index in range(120)],
                pub_date="Thu, 20 Aug 2026 06:00:00 GMT",
            )
            + _item(
                title="An unrelated fresh report",
                slug="unrelated",
                body_words=[f"other-{index}" for index in range(90)],
                pub_date="Thu, 20 Aug 2026 06:00:00 GMT",
            )
        )
        result = generate_edition(
            configuration,
            state_path=tmp_path / "state.sqlite3",
            output_directory=tmp_path / "editions",
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260820T040000Z-REPUBBBB",
            generated_at=datetime(2026, 8, 20, 4, tzinfo=UTC),
        )

    assert result.article_count == 1
    rendered = _epub_text(result.receipt.path)
    assert "An unrelated fresh report" in rendered
    assert title not in rendered
    assert "ARTICLE_REPUBLISHED_TITLE_SUPPRESSED" in _diagnostic_codes(tmp_path / "diagnostics")


@pytest.mark.acceptance
@pytest.mark.epubcheck
def test_a_material_update_of_the_same_url_still_returns(tmp_path: Path) -> None:
    """The republished-title suppression is about a second identity. A genuine revision of
    the delivered URL keeps its identity and must keep flowing as an update."""

    title = "En rapport som uppdateras"
    holder = {
        "xml": _feed(
            _item(
                title=title,
                slug="uppdateras",
                body_words=[f"first-{index}" for index in range(200)],
                pub_date="Wed, 19 Aug 2026 06:00:00 GMT",
            )
        )
    }
    with _mutable_feed_server(holder) as server:
        configuration = _configuration(
            tmp_path,
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
{_EVIDENCE}
publications:
  - id: morning
    title: Morning Briefing
    language: en
    budget: {{max_articles: 3, min_articles: 1}}
    sections:
      - id: world
        title: World
        sources: [fixture]
""".lstrip(),
        )
        generate_edition(
            configuration,
            state_path=tmp_path / "state.sqlite3",
            output_directory=tmp_path / "editions",
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260819T040000Z-UPDATAAA",
            generated_at=datetime(2026, 8, 19, 4, tzinfo=UTC),
        )

        holder["xml"] = _feed(
            _item(
                title=title,
                slug="uppdateras",
                body_words=[f"rewritten-{index}" for index in range(200)],
                pub_date="Thu, 20 Aug 2026 06:00:00 GMT",
            )
        )
        result = generate_edition(
            configuration,
            state_path=tmp_path / "state.sqlite3",
            output_directory=tmp_path / "editions",
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260820T040000Z-UPDATBBB",
            generated_at=datetime(2026, 8, 20, 4, tzinfo=UTC),
        )

    assert result.article_count == 1
    rendered = _epub_text(result.receipt.path)
    assert title in rendered
    assert "Updated since your previous Edition" in rendered


@pytest.mark.acceptance
@pytest.mark.epubcheck
def test_a_policy_age_window_keeps_stale_reporting_out(tmp_path: Path) -> None:
    """Observed live: a starved Section reached a month back into the feed and delivered a
    notice for an exhibition that had already closed. Partial beats stale."""

    feed_xml = _feed(
        _item(
            title="Fresh report",
            slug="fresh",
            body_words=[f"fresh-{index}" for index in range(90)],
            pub_date="Thu, 20 Aug 2026 06:00:00 GMT",
        )
        + _item(
            title="Exhibition until 19 July",
            slug="stale",
            body_words=[f"stale-{index}" for index in range(90)],
            pub_date="Tue, 14 Jul 2026 06:00:00 GMT",
        )
    )
    with _feed_server(feed_xml) as server:
        configuration = _configuration(
            tmp_path,
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
{_EVIDENCE}
publications:
  - id: morning
    title: Morning Briefing
    language: en
    budget: {{max_articles: 3, min_articles: 1}}
    policies:
      recent:
        type: interest
        minimum_sources: 1
        single_source_cap: 1.0
        max_age_days: 14
    sections:
      - id: world
        title: World
        policy: recent
        sources: [fixture]
""".lstrip(),
        )
        result = generate_edition(
            configuration,
            state_path=tmp_path / "state.sqlite3",
            output_directory=tmp_path / "editions",
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260821T040000Z-STALEAAA",
            generated_at=datetime(2026, 8, 21, 4, tzinfo=UTC),
        )

    assert result.article_count == 1
    rendered = _epub_text(result.receipt.path)
    assert "Fresh report" in rendered
    assert "Exhibition until 19 July" not in rendered


@pytest.mark.acceptance
@pytest.mark.epubcheck
def test_publication_notes_speak_the_publication_language(tmp_path: Path) -> None:
    """Observed live: 'Some reporting from X was unavailable' inside a Swedish Edition."""

    feed_xml = _feed(
        _item(
            title="En fungerande rapport",
            slug="fungerar",
            body_words=[f"svensk-{index}" for index in range(90)],
            pub_date="Thu, 20 Aug 2026 06:00:00 GMT",
        )
    )
    with _feed_server(feed_xml) as server:
        configuration = _configuration(
            tmp_path,
            f"""
version: 1
sources:
  fixture:
    title: Fixture News
    default_article_language: sv
    publisher_id: fixture-publisher
    allowed_publisher_origins: [https://publisher.example]
    feed_url: http://127.0.0.1:{server.server_port}/feed.xml
    acquisition: feed
    llm_processing: local_only
{_EVIDENCE}
  broken:
    title: Trasig Källa
    default_article_language: sv
    publisher_id: trasig-publisher
    allowed_publisher_origins: [https://publisher.example]
    feed_url: http://127.0.0.1:{server.server_port}/missing.xml
    acquisition: feed
    llm_processing: local_only
{_EVIDENCE}
publications:
  - id: morgon
    title: Morgonutgåvan
    language: sv
    budget: {{max_articles: 3, min_articles: 1}}
    sections:
      - id: world
        title: Världen
        sources: [fixture, broken]
""".lstrip(),
        )
        result = generate_edition(
            configuration,
            state_path=tmp_path / "state.sqlite3",
            output_directory=tmp_path / "editions",
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260821T040000Z-NOTESAAA",
            generated_at=datetime(2026, 8, 21, 4, tzinfo=UTC),
        )

    rendered = _epub_text(result.receipt.path)
    assert "Viss rapportering från Trasig Källa var inte tillgänglig" in rendered
    assert "Some reporting from" not in rendered
