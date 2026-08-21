"""Near Misses: the weekly recovering the week's eligible-but-unselected Articles.

A Saturday Run acquires from the same live feeds as any daily Run, so without help it can
only carry what is still in them — Monday's near-miss has usually fallen off by Saturday.
These tests cover the record kept for that (body-free by construction), the configuration
gate on who may read it, the recovery route itself, and — most importantly — that the
recovery fetch inherits every safety property of the ordinary acquisition path.
"""

from __future__ import annotations

import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic import ValidationError

from epub_news_feeder.acquisition import (
    AcquisitionMode,
    EligibilityEvidence,
    SourceClient,
    SourceRequest,
)
from epub_news_feeder.application import generate_edition
from epub_news_feeder.config import load_config
from epub_news_feeder.models import Configuration
from epub_news_feeder.state import StateStore

MONDAY = datetime(2026, 8, 10, 6, tzinfo=UTC)
SATURDAY = MONDAY + timedelta(days=5)

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


# ---------------------------------------------------------------------------- state


def _observe(state: StateStore, *, url: str, title: str, when: datetime) -> str:
    observed = state.observe_article(
        source_id="source-one",
        publisher_id="publisher",
        canonical_url=url,
        guid=url,
        title=title,
        author=None,
        normalized_body=f"a complete publisher article body about {title} " * 20,
        observed_at=when,
    )
    return observed.article_id


def test_near_misses_round_trip_distinct_and_windowed(tmp_path: Path) -> None:
    """Recording, the since-window, and one row per Article however many Runs saw it."""

    with StateStore(tmp_path / "state.sqlite3", environment="test") as state:
        monday_miss = _observe(
            state, url="https://publisher.example/monday", title="Monday", when=MONDAY
        )
        tuesday_miss = _observe(
            state,
            url="https://publisher.example/tuesday",
            title="Tuesday",
            when=MONDAY + timedelta(days=1),
        )
        state.record_near_misses("daily", [(monday_miss, "source-one")], recorded_at=MONDAY)
        state.record_near_misses(
            "daily",
            [(monday_miss, "source-one"), (tuesday_miss, "source-one")],
            recorded_at=MONDAY + timedelta(days=1),
        )

        rows = state.near_misses(("daily",), since=MONDAY - timedelta(days=1))
        # Distinct by Article, newest recorded first, canonical URL joined from `articles`.
        assert rows == [
            (monday_miss, "source-one", "https://publisher.example/monday"),
            (tuesday_miss, "source-one", "https://publisher.example/tuesday"),
        ] or rows == [
            (tuesday_miss, "source-one", "https://publisher.example/tuesday"),
            (monday_miss, "source-one", "https://publisher.example/monday"),
        ]
        # The re-record moved Monday's recorded_at forward, so both sit inside a
        # one-day window that Monday's original recording would have missed.
        assert len(state.near_misses(("daily",), since=MONDAY + timedelta(hours=12))) == 2
        # A window in the future sees nothing.
        assert state.near_misses(("daily",), since=MONDAY + timedelta(days=2)) == []
        # A Publication that recorded nothing answers nothing.
        assert state.near_misses(("weekly",), since=MONDAY - timedelta(days=1)) == []
        assert state.near_misses((), since=MONDAY - timedelta(days=1)) == []


def test_near_misses_newest_recorded_first_is_the_order(tmp_path: Path) -> None:
    """Recovery spends a bounded fetch budget, so the order must favour the fresh end."""

    with StateStore(tmp_path / "state.sqlite3", environment="test") as state:
        older = _observe(state, url="https://publisher.example/old", title="Old", when=MONDAY)
        newer = _observe(
            state,
            url="https://publisher.example/new",
            title="New",
            when=MONDAY + timedelta(days=2),
        )
        state.record_near_misses("daily", [(older, "source-one")], recorded_at=MONDAY)
        state.record_near_misses(
            "daily", [(newer, "source-one")], recorded_at=MONDAY + timedelta(days=2)
        )

        rows = state.near_misses(("daily",), since=MONDAY - timedelta(days=1))
        assert [article_id for article_id, _, _ in rows] == [newer, older]


def test_recording_prunes_near_misses_older_than_fourteen_days(tmp_path: Path) -> None:
    """The table is a short recovery window, not an archive; recording keeps it that way."""

    with StateStore(tmp_path / "state.sqlite3", environment="test") as state:
        stale = _observe(state, url="https://publisher.example/stale", title="Stale", when=MONDAY)
        state.record_near_misses("daily", [(stale, "source-one")], recorded_at=MONDAY)

        later = MONDAY + timedelta(days=15)
        fresh = _observe(state, url="https://publisher.example/fresh", title="Fresh", when=later)
        state.record_near_misses("daily", [(fresh, "source-one")], recorded_at=later)

        rows = state.near_misses(("daily",), since=MONDAY - timedelta(days=1))
        assert [article_id for article_id, _, _ in rows] == [fresh]


@pytest.mark.security
def test_near_miss_records_are_body_free(tmp_path: Path) -> None:
    """A Near Miss is a pointer, never a copy: no publisher text may reach the database."""

    body_sentence = "a distinctive publisher sentence that must never persist"
    state_path = tmp_path / "state.sqlite3"
    with StateStore(state_path, environment="test") as state:
        observed = state.observe_article(
            source_id="source-one",
            publisher_id="publisher",
            canonical_url="https://publisher.example/private",
            guid="private",
            title="A near-missed report",
            author=None,
            normalized_body=f"{body_sentence} " * 40,
            observed_at=MONDAY,
        )
        state.record_near_misses("daily", [(observed.article_id, "source-one")], recorded_at=MONDAY)

    database_bytes = state_path.read_bytes()
    assert b"distinctive publisher sentence" not in database_bytes
    assert body_sentence.encode() not in database_bytes


# ---------------------------------------------------------------------------- models


def _configuration_model(publications: list[dict[str, object]]) -> Configuration:
    return Configuration.model_validate(
        {
            "version": 1,
            "sources": {
                "source-one": {
                    "title": "Source one",
                    "feed_url": "https://publisher.example/feed.xml",
                }
            },
            "publications": publications,
        }
    )


def _publication_model(publication_id: str, **extra: object) -> dict[str, object]:
    return {
        "id": publication_id,
        "title": publication_id.title(),
        "sections": [
            {"id": "section-one", "title": "Section one", "sources": ["source-one"]},
        ],
        **extra,
    }


def test_recovery_without_a_history_reference_is_rejected_at_load() -> None:
    """A Publication with nobody to recover from is a configuration error, not a no-op."""

    with pytest.raises(ValidationError, match="recovers_near_misses"):
        _configuration_model([_publication_model("weekly", recovers_near_misses=True)])


def test_recovery_with_a_history_reference_is_accepted() -> None:
    configuration = _configuration_model(
        [
            _publication_model("weekly", reads_history_from=["daily"], recovers_near_misses=True),
            _publication_model("daily"),
        ]
    )
    weekly = next(item for item in configuration.publications if item.id == "weekly")
    assert weekly.recovers_near_misses is True


def test_no_publication_recovers_near_misses_by_default() -> None:
    configuration = _configuration_model([_publication_model("daily")])
    assert configuration.publications[0].recovers_near_misses is False


# ------------------------------------------------------------------- acquisition safety


def _evidence(now: datetime) -> EligibilityEvidence:
    return EligibilityEvidence(
        evidence_id="fixture-evidence",
        reviewed_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=30),
        feed_acquisition="allow",
        page_acquisition="allow",
        retention="allow",
        private_distribution="allow",
        local_llm="allow",
        remote_llm="unknown",
    )


@contextmanager
def _publisher_site(pages: dict[str, str], hits: list[str]) -> Iterator[ThreadingHTTPServer]:
    """Serve robots, a feed, and article pages, logging every request path."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            if self.path == "/robots.txt":
                payload, status = b"User-agent: *\nAllow: /\n", 200
            elif self.path in pages:
                payload, status = pages[self.path].encode(), 200
            else:
                payload, status = b"not found", 404
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
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


def _page(title: str, words: list[str]) -> str:
    body = " ".join(words) + "."
    return (
        f"<html><head><title>{title}</title></head>"
        f"<body><article><p>{body}</p></article></body></html>"
    )


def _request(server: ThreadingHTTPServer, now: datetime, **overrides: object) -> SourceRequest:
    origin = f"http://127.0.0.1:{server.server_port}"
    values: dict[str, object] = {
        "source_id": "fixture",
        "publisher_id": "fixture-publisher",
        "title": "Fixture News",
        "feed_url": f"{origin}/feed.xml",
        "mode": AcquisitionMode.WEB,
        "llm_processing": "local_only",
        "evidence": _evidence(now),
        "allowed_publisher_origins": (origin,),
    }
    values.update(overrides)
    return SourceRequest(**values)  # type: ignore[arg-type]


@pytest.mark.security
def test_acquire_article_refuses_a_url_outside_the_publisher_origins(tmp_path: Path) -> None:
    """The stored canonical URL is data. If it no longer sits inside the publisher's
    allowed origins, the recovery route must refuse without a single network request."""

    now = datetime(2026, 8, 15, 6, tzinfo=UTC)
    hits: list[str] = []
    lure_hits: list[str] = []
    with (
        _publisher_site({}, hits) as server,
        _publisher_site({"/lure": _page("Lure", ["word"] * 90)}, lure_hits) as lure,
    ):
        client = SourceClient(now=lambda: now)
        try:
            request = _request(server, now)
            # Another loopback origin: exactly the shape SSRF pinning exists to refuse.
            assert (
                client.acquire_article(request, f"http://127.0.0.1:{lure.server_port}/lure") is None
            )
            # A public host outside the allowlist is refused before resolution.
            assert client.acquire_article(request, "https://evil.example/lure") is None
        finally:
            client.close()
    assert lure_hits == [], "the out-of-origin URL must never be fetched"
    assert hits == [], "a refused URL must not trigger any fetch at all"


@pytest.mark.security
def test_acquire_article_refuses_when_page_evidence_denies(tmp_path: Path) -> None:
    """Recovery is a page fetch, so it obeys `page_acquisition` exactly as the feed
    route does — and a feed-mode or metadata-only Source, whose evidence never
    contemplated page fetches at all, is skipped outright."""

    now = datetime(2026, 8, 15, 6, tzinfo=UTC)
    hits: list[str] = []
    page = {"/reports/miss": _page("A near-missed report", ["word"] * 90)}
    with _publisher_site(page, hits) as server:
        url = f"http://127.0.0.1:{server.server_port}/reports/miss"
        client = SourceClient(now=lambda: now)
        try:
            denied = replace(_evidence(now), page_acquisition="deny")
            assert client.acquire_article(_request(server, now, evidence=denied), url) is None
            assert (
                client.acquire_article(_request(server, now, mode=AcquisitionMode.FEED), url)
                is None
            )
            assert (
                client.acquire_article(
                    _request(server, now, mode=AcquisitionMode.METADATA_ONLY), url
                )
                is None
            )
        finally:
            client.close()
    assert hits == [], "a denied or feed-only Source must never reach the page"


@pytest.mark.security
def test_acquire_article_obeys_robots_on_the_recovery_route(tmp_path: Path) -> None:
    """robots.txt governs a Saturday re-fetch exactly as it governs a Monday fetch."""

    now = datetime(2026, 8, 15, 6, tzinfo=UTC)
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            if self.path == "/robots.txt":
                payload, status = b"User-agent: *\nDisallow: /reports/\n", 200
            else:
                payload, status = _page("Disallowed", ["word"] * 90).encode(), 200
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = SourceClient(now=lambda: now)
        try:
            url = f"http://127.0.0.1:{server.server_port}/reports/miss"
            assert client.acquire_article(_request(server, now), url) is None
        finally:
            client.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    assert "/reports/miss" not in hits, "a robots-disallowed page must never be fetched"


def test_acquire_article_returns_the_page_as_an_ordinary_article(tmp_path: Path) -> None:
    now = datetime(2026, 8, 15, 6, tzinfo=UTC)
    hits: list[str] = []
    words = [f"recovered-{index}" for index in range(90)]
    page = {"/reports/miss": _page("A near-missed report", words)}
    with _publisher_site(page, hits) as server:
        client = SourceClient(now=lambda: now)
        try:
            url = f"http://127.0.0.1:{server.server_port}/reports/miss"
            acquired = client.acquire_article(_request(server, now), url)
        finally:
            client.close()
    assert acquired is not None
    assert acquired.title == "A near-missed report"
    assert acquired.body is not None and "recovered-5" in acquired.body
    assert acquired.classification == "verified_page_body"


def test_acquire_article_skips_a_page_that_has_become_a_teaser(tmp_path: Path) -> None:
    """A publisher can close the paywall between Monday's near miss and Saturday's
    recovery. The page then yields a mid-sentence stub — the very shape teaser demotion
    exists to keep out of Article Slots — so recovery skips it rather than delivering it."""

    now = datetime(2026, 8, 15, 6, tzinfo=UTC)
    hits: list[str] = []
    stub = " ".join(f"teaser-{index}" for index in range(120))
    page = {
        "/reports/miss": (
            "<html><head><title>A paywalled report</title></head>"
            f"<body><article><p>{stub}</p></article></body></html>"
        )
    }
    with _publisher_site(page, hits) as server:
        client = SourceClient(now=lambda: now)
        try:
            url = f"http://127.0.0.1:{server.server_port}/reports/miss"
            acquired = client.acquire_article(_request(server, now), url)
        finally:
            client.close()
    assert acquired is None


# --------------------------------------------------------------------------- pipeline


def _feed(server_port: int, items: list[tuple[str, str, str]]) -> str:
    entries = "".join(
        f"<item><title>{title}</title>"
        f"<link>http://127.0.0.1:{server_port}/reports/{slug}</link>"
        f"<guid>{slug}</guid><pubDate>{pub_date}</pubDate></item>"
        for title, slug, pub_date in items
    )
    return (
        '<?xml version="1.0"?><rss version="2.0">'
        f"<channel><title>Fixture News</title>{entries}</channel></rss>"
    )


def _yaml_configuration(tmp_path: Path, server_port: int) -> Configuration:
    config_path = tmp_path / "publication.yaml"
    config_path.write_text(
        f"""
version: 1
sources:
  fixture:
    title: Fixture News
    publisher_id: fixture-publisher
    allowed_publisher_origins: [http://127.0.0.1:{server_port}]
    feed_url: http://127.0.0.1:{server_port}/feed.xml
    acquisition: web
    llm_processing: local_only
{_EVIDENCE}
publications:
  - id: daily
    title: Morning Briefing
    language: en
    budget: {{max_articles: 1, min_articles: 1}}
    sections:
      - id: world
        title: World
        sources: [fixture]
  - id: weekly
    title: Weekly Briefing
    language: en
    reads_history_from: [daily]
    recovers_near_misses: true
    budget: {{max_articles: 5, min_articles: 1}}
    sections:
      - id: world
        title: World
        sources: [fixture]
""".lstrip(),
        encoding="utf-8",
    )
    return load_config(config_path)


def _epub_text(epub_path: Path) -> str:
    with zipfile.ZipFile(epub_path) as archive:
        return "".join(
            archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if name.endswith(".xhtml")
        )


@pytest.mark.acceptance
@pytest.mark.epubcheck
def test_the_weekly_recovers_a_near_miss_the_feed_no_longer_carries(tmp_path: Path) -> None:
    """Monday's daily had room for one of two eligible Articles. By Saturday the loser has
    fallen off the feed, but the publisher still serves its page — so the weekly, which
    reads the daily's history, re-acquires it by its stored canonical URL and carries it."""

    pages = {
        "/reports/selected": _page(
            "The selected report", [f"selected-{index}" for index in range(120)]
        ),
        "/reports/near-miss": _page(
            "The near-missed report", [f"missed-{index}" for index in range(90)]
        ),
    }
    hits: list[str] = []
    with _publisher_site(pages, hits) as server:
        pages["/feed.xml"] = _feed(
            server.server_port,
            [
                ("The selected report", "selected", "Mon, 10 Aug 2026 05:00:00 GMT"),
                ("The near-missed report", "near-miss", "Mon, 10 Aug 2026 04:00:00 GMT"),
            ],
        )
        configuration = _yaml_configuration(tmp_path, server.server_port)
        generate_edition(
            configuration,
            state_path=tmp_path / "state.sqlite3",
            output_directory=tmp_path / "editions",
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260810T040000Z-DAILYAAA",
            generated_at=MONDAY,
            publication_id="daily",
        )

        # Saturday: the feed has moved on entirely; only the pages remain.
        pages["/feed.xml"] = _feed(server.server_port, [])
        result = generate_edition(
            configuration,
            state_path=tmp_path / "state.sqlite3",
            output_directory=tmp_path / "editions",
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260815T050000Z-WEEKAAAA",
            generated_at=SATURDAY,
            publication_id="weekly",
        )

    assert result.article_count == 1
    rendered = _epub_text(result.receipt.path)
    assert "The near-missed report" in rendered
    assert "missed-5" in rendered
    assert "The selected report" not in rendered, "the daily already delivered it"


@pytest.mark.acceptance
@pytest.mark.epubcheck
def test_a_near_miss_the_daily_later_delivered_is_never_recovered(tmp_path: Path) -> None:
    """Tuesday's daily caught up with Monday's near miss. Saturday owes the reader nothing."""

    pages = {
        "/reports/first": _page("The first report", [f"first-{index}" for index in range(120)]),
        "/reports/second": _page("The second report", [f"second-{index}" for index in range(90)]),
    }
    hits: list[str] = []
    with _publisher_site(pages, hits) as server:
        pages["/feed.xml"] = _feed(
            server.server_port,
            [
                ("The first report", "first", "Mon, 10 Aug 2026 05:00:00 GMT"),
                ("The second report", "second", "Mon, 10 Aug 2026 04:00:00 GMT"),
            ],
        )
        configuration = _yaml_configuration(tmp_path, server.server_port)
        generate_edition(
            configuration,
            state_path=tmp_path / "state.sqlite3",
            output_directory=tmp_path / "editions",
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260810T040000Z-DAILYAAA",
            generated_at=MONDAY,
            publication_id="daily",
        )

        # Tuesday: only Monday's near miss remains in the feed, so the daily delivers it.
        pages["/feed.xml"] = _feed(
            server.server_port,
            [("The second report", "second", "Mon, 10 Aug 2026 04:00:00 GMT")],
        )
        generate_edition(
            configuration,
            state_path=tmp_path / "state.sqlite3",
            output_directory=tmp_path / "editions",
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260811T040000Z-DAILYBBB",
            generated_at=MONDAY + timedelta(days=1),
            publication_id="daily",
        )

        pages["/feed.xml"] = _feed(server.server_port, [])
        hits.clear()
        with pytest.raises(Exception, match=r"publication minimum"):
            generate_edition(
                configuration,
                state_path=tmp_path / "state.sqlite3",
                output_directory=tmp_path / "editions",
                diagnostics_directory=tmp_path / "diagnostics",
                run_id="20260815T050000Z-WEEKAAAA",
                generated_at=SATURDAY,
                publication_id="weekly",
            )

    assert "/reports/second" not in hits, "a delivered Article must not be re-fetched"
    assert "/reports/first" not in hits, "a delivered Article must not be re-fetched"


@pytest.mark.acceptance
@pytest.mark.epubcheck
def test_a_recovered_near_miss_outside_the_allowed_origins_is_not_fetched(
    tmp_path: Path,
) -> None:
    """State is data, not authority. A stored canonical URL that no longer resolves inside
    the publisher's allowed origins must be refused by the recovery route without a fetch,
    exactly as the feed route would have refused it."""

    pages = {
        "/reports/fresh": _page("A fresh report", [f"fresh-{index}" for index in range(120)]),
    }
    hits: list[str] = []
    lure_hits: list[str] = []
    with (
        _publisher_site(pages, hits) as server,
        _publisher_site({"/lure": _page("Lure", ["word"] * 90)}, lure_hits) as lure,
    ):
        pages["/feed.xml"] = _feed(
            server.server_port,
            [("A fresh report", "fresh", "Sat, 15 Aug 2026 04:00:00 GMT")],
        )
        configuration = _yaml_configuration(tmp_path, server.server_port)

        # Seed the daily's history directly: an Article whose stored canonical URL points at
        # another origin, recorded as a Near Miss. The acquisition route would never have
        # produced this row, which is exactly why the recovery route must re-check it.
        with StateStore(tmp_path / "state.sqlite3", environment="local") as state:
            observed = state.observe_article(
                source_id="fixture",
                publisher_id="fixture-publisher",
                canonical_url=f"http://127.0.0.1:{lure.server_port}/lure",
                guid="lure",
                title="A planted pointer",
                author=None,
                normalized_body="planted body text " * 40,
                observed_at=MONDAY,
            )
            state.record_near_misses(
                "daily", [(observed.article_id, "fixture")], recorded_at=MONDAY
            )

        result = generate_edition(
            configuration,
            state_path=tmp_path / "state.sqlite3",
            output_directory=tmp_path / "editions",
            diagnostics_directory=tmp_path / "diagnostics",
            run_id="20260815T050000Z-WEEKAAAA",
            generated_at=SATURDAY,
            publication_id="weekly",
        )

    assert lure_hits == [], "the out-of-origin URL must never be fetched"
    rendered = _epub_text(result.receipt.path)
    assert "A planted pointer" not in rendered
    assert "A fresh report" in rendered
