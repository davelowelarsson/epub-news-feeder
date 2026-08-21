from __future__ import annotations

import gzip
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import feedparser
import pytest

from epub_news_feeder.acquisition import (
    AcquisitionMode,
    EligibilityEvidence,
    SourceClient,
    SourceRequest,
    _decoded_feed,
    _html_blocks,
)


@dataclass
class FixtureSite:
    base_url: str
    routes: dict[str, tuple[int, str, bytes]]
    hits: list[str] = field(default_factory=list)
    headers: dict[str, dict[str, str]] = field(default_factory=dict)


@contextmanager
def fixture_site(
    routes: dict[str, tuple[int, str, bytes]],
) -> Iterator[FixtureSite]:
    site = FixtureSite("", routes)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            site.hits.append(self.path)
            status, content_type, body = site.routes.get(
                self.path, (404, "text/plain", b"not found")
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for name, value in site.headers.get(self.path, {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    site.base_url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield site
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def evidence(now: datetime) -> EligibilityEvidence:
    return EligibilityEvidence(
        evidence_id="fixture-20260809",
        reviewed_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=29),
        feed_acquisition="allow",
        page_acquisition="allow",
        retention="allow",
        private_distribution="allow",
        local_llm="allow",
        remote_llm="unknown",
    )


@pytest.mark.acceptance
def test_ticket_05_compressed_responses_are_decoded_once() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    feed = b"""<rss version="2.0"><channel><title>Compressed</title><item>
    <title>Linked report</title><link>REPLACE/report</link><guid>compressed-1</guid>
    </item></channel></rss>"""
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", gzip.compress(b"User-agent: *\nAllow: /\n")),
            "/feed.xml": (200, "application/rss+xml", b""),
        }
    ) as site:
        site.routes["/feed.xml"] = (
            200,
            "application/rss+xml",
            gzip.compress(feed.replace(b"REPLACE", site.base_url.encode())),
        )
        site.headers["/robots.txt"] = {"Content-Encoding": "gzip"}
        site.headers["/feed.xml"] = {"Content-Encoding": "gzip"}

        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="compressed",
                publisher_id="publisher",
                title="Compressed",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.METADATA_ONLY,
                llm_processing="disabled",
                evidence=evidence(now),
            )
        )

    assert outcome.code == "SOURCE_OK"
    assert [article.title for article in outcome.articles] == ["Linked report"]


@pytest.mark.acceptance
def test_ticket_04_exact_robots_group_denies_before_feed_fetch() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    robots = b"""User-agent: *
Allow: /

User-agent: epub-news-feeder
Disallow: /feed.xml
"""
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", robots),
            "/feed.xml": (200, "application/rss+xml", b"must not be fetched"),
        }
    ) as site:
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="blocked",
                publisher_id="publisher",
                title="Blocked Source",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.FEED,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    assert outcome.articles == ()
    assert outcome.code == "SOURCE_ROBOTS_DENIED"
    assert site.hits == ["/robots.txt"]


@pytest.mark.acceptance
def test_ticket_04_malformed_robots_fails_closed_before_feed_fetch() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"this is not a robots record"),
            "/feed.xml": (200, "application/rss+xml", b"must not be fetched"),
        }
    ) as site:
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="malformed-rep",
                publisher_id="publisher",
                title="Malformed REP",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.FEED,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    assert outcome.code == "SOURCE_ROBOTS_INVALID"
    assert outcome.articles == ()
    assert site.hits == ["/robots.txt"]


@pytest.mark.acceptance
def test_ticket_04_robots_redirect_fails_closed_before_feed_fetch() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    with fixture_site(
        {
            "/robots.txt": (302, "text/plain", b""),
            "/feed.xml": (200, "application/rss+xml", b"must not be fetched"),
        }
    ) as site:
        site.headers["/robots.txt"] = {"Location": "/different-policy"}
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="redirected-rep",
                publisher_id="publisher",
                title="Redirected REP",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.FEED,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    assert outcome.code == "SOURCE_ROBOTS_UNAVAILABLE"
    assert outcome.articles == ()
    assert site.hits == ["/robots.txt"]


@pytest.mark.acceptance
def test_robots_rules_match_percent_decoded_paths() -> None:
    """RFC 9309 matches percent-decoded octets: "/%70rivate" is "/private", and an encoded
    byte must not sidestep a rule the publisher wrote in plain text."""

    now = datetime(2026, 8, 9, tzinfo=UTC)
    robots = b"User-agent: *\nDisallow: /private\n"
    feed = b"""<rss version="2.0"><channel><title>Encoded</title><item>
    <title>Behind the rule</title><link>REPLACE/%70rivate/report</link><guid>enc-1</guid>
    </item></channel></rss>"""
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", robots),
            "/feed.xml": (200, "application/rss+xml", b""),
            "/%70rivate/report": (200, "text/html", b"<article><p>must not be read</p></article>"),
        }
    ) as site:
        site.routes["/feed.xml"] = (
            200,
            "application/rss+xml",
            feed.replace(b"REPLACE", site.base_url.encode()),
        )
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="encoded",
                publisher_id="publisher",
                title="Encoded",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.WEB,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    assert outcome.articles == ()
    assert outcome.omitted == 1
    assert not any("rivate" in hit for hit in site.hits), "the page must never be fetched"


@pytest.mark.acceptance
def test_ticket_04_robots_wildcards_and_end_anchors_follow_rfc_matching() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    robots = b"""User-agent: epub-news-feeder
Disallow: /private/*.xml$
Allow: /private/public-*.xml$
"""
    empty_feed = b'<rss version="2.0"><channel><title>Empty</title></channel></rss>'
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", robots),
            "/private/public-news.xml": (200, "application/rss+xml", empty_feed),
            "/private/news.xml?view=full": (200, "application/rss+xml", empty_feed),
            "/private/news.xml": (200, "application/rss+xml", b"must not be fetched"),
        }
    ) as site:
        client = SourceClient(now=lambda: now)

        def acquire(path: str) -> str:
            return client.acquire(
                SourceRequest(
                    source_id=path,
                    publisher_id="publisher",
                    title="REP fixture",
                    feed_url=f"{site.base_url}{path}",
                    mode=AcquisitionMode.FEED,
                    llm_processing="local_only",
                    evidence=evidence(now),
                )
            ).code

        allowed_by_specific_rule = acquire("/private/public-news.xml")
        allowed_because_anchor_does_not_match_query = acquire("/private/news.xml?view=full")
        denied_at_end = acquire("/private/news.xml")
        client.close()

    assert allowed_by_specific_rule == "SOURCE_OK"
    assert allowed_because_anchor_does_not_match_query == "SOURCE_OK"
    assert denied_at_end == "SOURCE_ROBOTS_DENIED"
    assert "/private/news.xml" not in site.hits


@pytest.mark.acceptance
def test_ticket_05_feed_page_and_metadata_routes_never_use_previews_as_articles() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    full_text = " ".join(f"verified-word-{index}" for index in range(160)) + "."
    page_text = " ".join(f"page-word-{index}" for index in range(160)) + "."
    full_feed = f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel><title>Full</title><item>
    <title>Full Article</title><link>https://publisher.example/full</link>
    <guid>full-1</guid><author>Reporter</author>
    <description>preview must not become body</description>
    <content:encoded><![CDATA[<p>{full_text}</p>]]></content:encoded>
  </item></channel>
</rss>""".encode()

    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/full.xml": (200, "application/rss+xml", full_feed),
            "/web.xml": (
                200,
                "application/rss+xml",
                b"""<rss version="2.0"><channel><title>Web</title><item>
                <title>Web Article</title><link>REPLACE/article</link><guid>web-1</guid>
                <description>preview must not become body</description>
                </item></channel></rss>""",
            ),
            "/metadata.xml": (
                200,
                "application/atom+xml",
                b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Metadata</title><entry>
                <title>Linked Audio</title><id>meta-1</id><link href="REPLACE/audio" />
                <updated>2026-08-09T06:00:00Z</updated><summary>forbidden retained summary</summary>
                </entry></feed>""",
            ),
            "/article": (
                200,
                "text/html",
                (
                    f"<html><body><article><h1>Web Article</h1>"
                    f"<p>{page_text}</p></article></body></html>"
                ).encode(),
            ),
        }
    ) as site:
        site.routes["/web.xml"] = (
            200,
            "application/rss+xml",
            site.routes["/web.xml"][2].replace(b"REPLACE", site.base_url.encode()),
        )
        site.routes["/metadata.xml"] = (
            200,
            "application/atom+xml",
            site.routes["/metadata.xml"][2].replace(b"REPLACE", site.base_url.encode()),
        )
        client = SourceClient(now=lambda: now)
        full = client.acquire(
            SourceRequest(
                source_id="full",
                publisher_id="publisher",
                title="Full",
                feed_url=f"{site.base_url}/full.xml",
                mode=AcquisitionMode.FEED,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )
        web = client.acquire(
            SourceRequest(
                source_id="web",
                publisher_id="publisher",
                title="Web",
                feed_url=f"{site.base_url}/web.xml",
                mode=AcquisitionMode.WEB,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )
        metadata = client.acquire(
            SourceRequest(
                source_id="metadata",
                title="Metadata",
                feed_url=f"{site.base_url}/metadata.xml",
                mode=AcquisitionMode.METADATA_ONLY,
                llm_processing="disabled",
                publisher_id="publisher",
                evidence=evidence(now),
            )
        )

    assert full.articles[0].body == full_text
    assert full.articles[0].classification == "verified_feed_body"
    assert web.articles[0].body == page_text
    assert web.articles[0].classification == "verified_page_body"
    assert "preview" not in full.articles[0].body
    assert "preview" not in web.articles[0].body
    assert metadata.articles[0].body is None
    assert metadata.articles[0].classification == "metadata_only"
    assert "forbidden retained summary" not in repr(metadata.articles[0])


@pytest.mark.acceptance
def test_feed_article_preserves_paragraphs_and_omits_raw_mermaid_diagrams() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    first = " ".join(f"first-{index}" for index in range(50)) + "."
    second = " ".join(f"second-{index}" for index in range(50)) + "."
    feed = f"""<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
    <channel><title>Structured</title><item><title>Structured report</title>
    <link>https://publisher.example/structured</link><guid>structured-1</guid>
    <content:encoded><![CDATA[
      <p>{first}</p>
      <pre><code>flowchart TD task--&gt;result</code></pre>
      <p>{second}</p>
    ]]></content:encoded></item></channel></rss>""".encode()
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (200, "application/rss+xml", feed),
        }
    ) as site:
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="structured",
                publisher_id="publisher.example",
                title="Structured",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.FEED,
                llm_processing="local_only",
                default_article_language="en",
                evidence=evidence(now),
            )
        )

    assert outcome.articles[0].body == f"{first}\n\n{second}"
    assert outcome.articles[0].language == "en"
    assert "flowchart" not in outcome.articles[0].body


@pytest.mark.acceptance
def test_feed_article_classifies_body_blocks_by_kind() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    feed = b"""<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
    <channel><title>Structured</title><item><title>Structured report</title>
    <link>https://publisher.example/structured</link><guid>structured-2</guid>
    <content:encoded><![CDATA[
      <p>Intro paragraph text.</p>
      <blockquote>A pull quote from a source.</blockquote>
      <ul><li>First list item.</li><li>Second list item.</li></ul>
      <pre><code>git status --short</code></pre>
      <pre><code>flowchart TD task--&gt;result</code></pre>
    ]]></content:encoded></item></channel></rss>"""
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (200, "application/rss+xml", feed),
        }
    ) as site:
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="structured-blocks",
                publisher_id="publisher.example",
                title="Structured",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.FEED,
                llm_processing="local_only",
                evidence=evidence(now),
                minimum_full_words=1,
            )
        )

    article = outcome.articles[0]
    assert [(block.kind, block.text) for block in article.blocks] == [
        ("paragraph", "Intro paragraph text."),
        ("quote", "A pull quote from a source."),
        ("list", "First list item."),
        ("list", "Second list item."),
        ("code", "git status --short"),
        ("diagram", "flowchart TD task-->result"),
    ]
    assert article.body == (
        "Intro paragraph text.\n\n"
        "A pull quote from a source.\n\n"
        "First list item.\n\n"
        "Second list item.\n\n"
        "git status --short"
    )
    assert "flowchart" not in article.body


@pytest.mark.acceptance
def test_metadata_only_article_carries_no_body_blocks() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    feed = b"""<rss version="2.0"><channel><title>Metadata</title><item>
    <title>Linked report</title><link>REPLACE/report</link>
    <guid>metadata-blocks-1</guid></item></channel></rss>"""
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (200, "application/rss+xml", b""),
        }
    ) as site:
        site.routes["/feed.xml"] = (
            200,
            "application/rss+xml",
            feed.replace(b"REPLACE", site.base_url.encode()),
        )
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="metadata-blocks",
                publisher_id="publisher",
                title="Metadata",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.METADATA_ONLY,
                llm_processing="disabled",
                evidence=evidence(now),
            )
        )

    assert outcome.articles[0].body is None
    assert outcome.articles[0].blocks == ()


@pytest.mark.acceptance
def test_auto_route_rejects_a_feed_teaser_and_fetches_the_complete_page() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    teaser = " ".join(f"teaser-{index}" for index in range(100))
    first = " ".join(f"complete-first-{index}" for index in range(80))
    second = " ".join(f"complete-second-{index}" for index in range(80)) + "."
    feed = f"""<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
    <channel><title>Auto</title><item><title>Complete report</title>
    <link>REPLACE/article</link><guid>auto-1</guid>
    <content:encoded><![CDATA[
      <p>{teaser}</p>
      <p><a href="REPLACE/article">Read full article</a></p>
      <p><a href="REPLACE/article#comments">Comments</a></p>
    ]]></content:encoded></item></channel></rss>""".encode()
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (200, "application/rss+xml", b""),
            "/article": (
                200,
                "text/html",
                f"<html><body><article><p>{first}</p><p>{second}</p></article></body></html>".encode(),
            ),
        }
    ) as site:
        site.routes["/feed.xml"] = (
            200,
            "application/rss+xml",
            feed.replace(b"REPLACE", site.base_url.encode()),
        )
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="auto",
                publisher_id="publisher",
                title="Auto",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.AUTO,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    assert outcome.articles[0].classification == "verified_page_body"
    assert outcome.articles[0].body == f"{first}\n\n{second}"
    assert "Read full article" not in outcome.articles[0].body
    assert "Comments" not in outcome.articles[0].body
    assert site.hits == ["/robots.txt", "/feed.xml", "/article"]


@pytest.mark.acceptance
def test_web_page_body_stays_byte_identical_to_paragraph_only_extraction() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    first = " ".join(f"page-first-{index}" for index in range(80))
    second = " ".join(f"page-second-{index}" for index in range(80)) + "."
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (
                200,
                "application/rss+xml",
                b'<rss version="2.0"><channel><title>Page</title><item>'
                b"<title>Page report</title><link>REPLACE/article</link>"
                b"<guid>page-body-1</guid></item></channel></rss>",
            ),
            "/article": (
                200,
                "text/html",
                (
                    f"<html><body><article><nav>Skip to content</nav>"
                    f"<p>{first}</p><p>{second}</p>"
                    f"</article></body></html>"
                ).encode(),
            ),
        }
    ) as site:
        site.routes["/feed.xml"] = (
            200,
            "application/rss+xml",
            site.routes["/feed.xml"][2].replace(b"REPLACE", site.base_url.encode()),
        )
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="page-body",
                publisher_id="publisher",
                title="Page",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.WEB,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    assert outcome.articles[0].body == f"{first}\n\n{second}"
    assert "Skip to content" not in outcome.articles[0].body


@pytest.mark.acceptance
def test_web_page_article_classifies_body_blocks_by_kind() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    page = b"""<html><body><article>
      <p>Intro paragraph text.</p>
      <blockquote>A pull quote from a source.</blockquote>
      <ul><li>First list item.</li><li>Second list item.</li></ul>
      <pre><code>git status --short</code></pre>
      <pre><code>flowchart TD task--&gt;result</code></pre>
    </article></body></html>"""
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (
                200,
                "application/rss+xml",
                b'<rss version="2.0"><channel><title>Page</title><item>'
                b"<title>Structured page</title><link>REPLACE/article</link>"
                b"<guid>page-blocks-1</guid></item></channel></rss>",
            ),
            "/article": (200, "text/html", page),
        }
    ) as site:
        site.routes["/feed.xml"] = (
            200,
            "application/rss+xml",
            site.routes["/feed.xml"][2].replace(b"REPLACE", site.base_url.encode()),
        )
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="page-blocks",
                publisher_id="publisher",
                title="Page",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.WEB,
                llm_processing="local_only",
                evidence=evidence(now),
                minimum_full_words=1,
            )
        )

    article = outcome.articles[0]
    assert article.classification == "verified_page_body"
    assert [(block.kind, block.text) for block in article.blocks] == [
        ("paragraph", "Intro paragraph text."),
        ("quote", "A pull quote from a source."),
        ("list", "First list item."),
        ("list", "Second list item."),
        ("code", "git status --short"),
        ("diagram", "flowchart TD task-->result"),
    ]
    assert article.body == (
        "Intro paragraph text.\n\n"
        "A pull quote from a source.\n\n"
        "First list item.\n\n"
        "Second list item.\n\n"
        "git status --short"
    )
    assert "flowchart" not in article.body


@pytest.mark.acceptance
@pytest.mark.parametrize(
    ("mode", "publisher_link"),
    [
        (AcquisitionMode.WEB, "http://10.0.0.1/private"),
        (AcquisitionMode.WEB, "http://169.254.169.254/latest/meta-data"),
        (AcquisitionMode.WEB, "http://127.0.0.1:9/private"),
        (AcquisitionMode.WEB, "https://attacker.example/article"),
        (AcquisitionMode.METADATA_ONLY, "javascript:alert(1)"),
        (AcquisitionMode.METADATA_ONLY, "https://user:secret@publisher.example/article"),
    ],
)
def test_ticket_05_unsafe_publisher_links_are_omitted_without_fetch(
    mode: AcquisitionMode, publisher_link: str
) -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    feed = f"""<rss version="2.0"><channel><title>Unsafe</title><item>
    <title>Unsafe target</title><link>{publisher_link}</link><guid>unsafe-1</guid>
    </item></channel></rss>""".encode()
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (200, "application/rss+xml", feed),
        }
    ) as site:
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="unsafe",
                publisher_id="publisher.example",
                title="Unsafe",
                feed_url=f"{site.base_url}/feed.xml",
                mode=mode,
                llm_processing="disabled",
                evidence=evidence(now),
                allowed_publisher_origins=("https://publisher.example",),
            )
        )

    assert outcome.articles == ()
    assert outcome.omitted == 1
    assert site.hits == ["/robots.txt", "/feed.xml"]


@pytest.mark.acceptance
def test_ticket_05_page_redirect_cannot_pivot_to_another_loopback_origin() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    with (
        fixture_site(
            {
                "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
                "/private": (200, "text/plain", b"must not be fetched"),
            }
        ) as target,
        fixture_site(
            {
                "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
                "/feed.xml": (
                    200,
                    "application/rss+xml",
                    b'<rss version="2.0"><channel><title>Redirect</title><item>'
                    b"<title>Redirect target</title><link>REPLACE/article</link>"
                    b"<guid>redirect-1</guid></item></channel></rss>",
                ),
                "/article": (302, "text/plain", b""),
            }
        ) as source,
    ):
        source.routes["/feed.xml"] = (
            200,
            "application/rss+xml",
            source.routes["/feed.xml"][2].replace(b"REPLACE", source.base_url.encode()),
        )
        source.headers["/article"] = {"Location": f"{target.base_url}/private"}
        client = SourceClient(now=lambda: now)
        outcome = client.acquire(
            SourceRequest(
                source_id="redirect",
                publisher_id="publisher",
                title="Redirect",
                feed_url=f"{source.base_url}/feed.xml",
                mode=AcquisitionMode.WEB,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )
        client.close()

    assert outcome.articles == ()
    assert target.hits == []


@pytest.mark.acceptance
def test_ticket_05_page_canonical_must_match_the_publisher_origin_policy() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    body = " ".join(f"publisher-word-{index}" for index in range(100))
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (
                200,
                "application/rss+xml",
                b'<rss version="2.0"><channel><title>Canonical</title><item>'
                b"<title>Canonical target</title><link>REPLACE/article</link>"
                b"<guid>canonical-1</guid></item></channel></rss>",
            ),
            "/article": (
                200,
                "text/html",
                (
                    '<html><head><link rel="canonical" href="https://attacker.example/item">'
                    f"</head><body><article><p>{body}</p></article></body></html>"
                ).encode(),
            ),
        }
    ) as site:
        site.routes["/feed.xml"] = (
            200,
            "application/rss+xml",
            site.routes["/feed.xml"][2].replace(b"REPLACE", site.base_url.encode()),
        )
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="canonical",
                publisher_id="publisher",
                title="Canonical",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.WEB,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    assert outcome.articles == ()
    assert site.hits == ["/robots.txt", "/feed.xml", "/article"]


@pytest.mark.acceptance
def test_ticket_05_oversized_response_fails_closed_at_the_download_boundary() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    oversized_feed = b"<rss>" + (b"x" * 1024) + b"</rss>"
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (200, "application/rss+xml", oversized_feed),
        }
    ) as site:
        outcome = SourceClient(now=lambda: now, max_response_bytes=256).acquire(
            SourceRequest(
                source_id="oversized",
                publisher_id="publisher",
                title="Oversized",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.FEED,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    assert outcome.code == "SOURCE_RESPONSE_TOO_LARGE"
    assert outcome.articles == ()


@pytest.mark.acceptance
def test_ticket_05_oversized_article_body_is_omitted() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    article_body = " ".join(f"article-word-{index}" for index in range(200))
    feed = f"""<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
    <channel><title>Body limit</title><item><title>Large body</title>
    <link>https://publisher.example/large</link><guid>large-1</guid>
    <content:encoded><![CDATA[<p>{article_body}</p>]]></content:encoded>
    </item></channel></rss>""".encode()
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (200, "application/rss+xml", feed),
        }
    ) as site:
        outcome = SourceClient(
            now=lambda: now,
            max_response_bytes=16 * 1024,
            max_article_body_bytes=512,
        ).acquire(
            SourceRequest(
                source_id="body-limit",
                publisher_id="publisher.example",
                title="Body limit",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.FEED,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    assert outcome.code == "SOURCE_PARTIAL"
    assert outcome.articles == ()
    assert outcome.omitted == 1


@pytest.mark.acceptance
@pytest.mark.parametrize("status", [401, 403, 451])
def test_ticket_05_access_control_responses_are_never_retried(status: int) -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (status, "text/plain", b"access controlled"),
        }
    ) as site:
        outcome = SourceClient(now=lambda: now, max_attempts=3).acquire(
            SourceRequest(
                source_id="access-controlled",
                publisher_id="publisher",
                title="Access controlled",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.FEED,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    assert outcome.code == "SOURCE_ACCESS_CONTROLLED"
    assert site.hits == ["/robots.txt", "/feed.xml"]


def test_a_single_bad_byte_does_not_corrupt_an_otherwise_utf8_feed() -> None:
    """One stray latin-1 byte must not re-read the whole feed through a guessed codepage.

    Observed live in Danstidningen's feed: it declares UTF-8 and is UTF-8 apart from a
    single 0xf6, and a parser left to sniff the payload abandoned UTF-8 for the entire
    document — turning every "å" and "ö" in the delivered Edition into mojibake.
    """

    payload = (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><item>'
        "<title>Lyssna på koreografen</title>"
        "<description>Det kan ses på Schweizisk tv och börjar direkt.</description>"
        "</item></channel></rss>"
    ).encode()
    # Splice in the raw latin-1 byte the publisher actually emits.
    corrupted = payload.replace(b"direkt", b"direkt\xf6")

    parsed = feedparser.parse(_decoded_feed(corrupted))

    assert parsed.entries
    assert parsed.entries[0].title == "Lyssna på koreografen"
    assert "på Schweizisk" in parsed.entries[0].description
    assert "börjar" in parsed.entries[0].description


def test_a_feed_that_is_honestly_another_encoding_still_reaches_the_parser_as_bytes() -> None:
    """A genuinely latin-1 feed is riddled with undecodable bytes, so the parser's own
    charset detection must be left to handle it rather than replacing half the text."""

    payload = (
        '<?xml version="1.0" encoding="ISO-8859-1"?><rss version="2.0"><channel><item>'
        "<title>Sm\xf6rg\xe5sbord i K\xf6penhamn n\xe4r v\xe5ren b\xf6rjar och \xe5skan g\xe5r"
        "</title></channel></rss>"
    ).encode("latin-1")

    assert isinstance(_decoded_feed(payload), bytes)


def _blocks(fragment: str) -> tuple[str, ...]:
    return tuple(block.text for block in _html_blocks(fragment))


def test_a_fixture_table_rendered_as_a_list_is_not_body_text() -> None:
    """Observed live: an SVT Sport report ended in a hundred four-word list items - a whole
    season's results - which read as three pages of nothing after the actual report."""

    fixtures = "".join(f"<li>Pitea vs Hacken {day}/8</li>" for day in range(1, 41))
    fragment = f"<div><p>Häcken gjorde processen kort med Piteå.</p><ul>{fixtures}</ul></div>"

    assert _blocks(fragment) == ("Häcken gjorde processen kort med Piteå.",)


def test_a_short_list_a_reader_wants_survives() -> None:
    steps = "".join(f"<li>Step {index}</li>" for index in range(1, 6))
    fragment = f"<div><p>Do this.</p><ol>{steps}</ol></div>"

    assert _blocks(fragment) == ("Do this.", *[f"Step {index}" for index in range(1, 6)])


def test_a_long_list_of_substantial_items_survives() -> None:
    """Length alone must not condemn a list — only a long run of uniformly tiny items."""

    items = "".join(
        f"<li>Point {index} explains something at a length a reader would actually "
        f"read, running well past a scoreline.</li>"
        for index in range(1, 21)
    )
    fragment = f"<div><ul>{items}</ul></div>"

    assert len(_blocks(fragment)) == 20


def test_a_bare_all_capitals_label_is_not_body_text() -> None:
    """Observed live: Open Source Malware articles opened with a styled bare "BLOG"."""

    fragment = "<div><p>BLOG</p><p>A new npm worm hit Keyv and cacheable this week.</p></div>"

    assert _blocks(fragment) == ("A new npm worm hit Keyv and cacheable this week.",)


def test_a_wordpress_feed_trailer_is_not_body_text() -> None:
    """Observed live: every Cyber Security News and WWF article ended in the WordPress
    trailer "The post <title> appeared first on <site>.", rendered as if it were prose."""

    fragment = (
        "<div><p>Microsoft confirmed the flaw is exploited in the wild.</p>"
        "<p>The post Microsoft Entra ID Remote Code Execution Vulnerability Exploited "
        "in the Wild appeared first on Cyber Security News.</p></div>"
    )

    assert _blocks(fragment) == ("Microsoft confirmed the flaw is exploited in the wild.",)


def test_prose_that_merely_mentions_a_trailer_phrase_survives() -> None:
    fragment = (
        "<div><p>The article appeared first on the publisher's own site before "
        "syndication picked it up, which is why The post label matters here.</p></div>"
    )

    assert len(_blocks(fragment)) == 1


def test_leading_navigation_chrome_is_not_body_text() -> None:
    """Observed live: Special Nest article bodies opened with the site menu — "Om oss",
    "Cookiepolicy", "Prenumeration", "Logga in" — rendered as a list before the article."""

    fragment = (
        "<div><ul><li>Om oss</li><li>Cookiepolicy</li><li>Prenumeration</li>"
        "<li>Logga in</li></ul><p>Skolstart stundar runt om i landet.</p></div>"
    )

    assert _blocks(fragment) == ("Skolstart stundar runt om i landet.",)


def test_a_short_list_after_prose_still_survives() -> None:
    fragment = (
        "<div><p>Pack these before the trip.</p>"
        "<ul><li>Passport</li><li>Charger</li><li>Water bottle</li></ul></div>"
    )

    assert _blocks(fragment) == (
        "Pack these before the trip.",
        "Passport",
        "Charger",
        "Water bottle",
    )


def test_a_trailing_run_of_related_headlines_is_not_body_text() -> None:
    """Observed live: Special Nest page bodies ended in the site's related-articles widget —
    seven headline list items that change over time, so every fetch read as a material
    update and the same article kept re-entering Editions."""

    fragment = (
        "<div><p>Adhd förekommer hos knappt tre procent av den vuxna befolkningen.</p>"
        "<ul><li>Kurs om adhd till nytta för föräldrar med egna symtom</li>"
        "<li>Hur ser personer med autism på att skaffa barn?</li>"
        "<li>Om Pans: ”Svårt att fastställa diagnosen”</li>"
        "<li>Motoriska problem starkt kopplat till NPF i ny studie</li></ul></div>"
    )

    assert _blocks(fragment) == (
        "Adhd förekommer hos knappt tre procent av den vuxna befolkningen.",
    )


def test_a_trailing_how_to_list_with_sentences_survives() -> None:
    fragment = (
        "<div><p>Gör så här inför loppet.</p>"
        "<ul><li>Ladda med kolhydrater kvällen före loppet.</li>"
        "<li>Värm upp i minst femton minuter före starten.</li>"
        "<li>Håll jämn fart under de första kilometrarna.</li></ul></div>"
    )

    assert len(_blocks(fragment)) == 4


def test_a_trailing_list_of_short_items_survives() -> None:
    fragment = (
        "<div><p>Pack these before the trip.</p>"
        "<ul><li>Passport and visa</li><li>Charger</li><li>Water bottle</li></ul></div>"
    )

    assert len(_blocks(fragment)) == 4


def test_a_stock_agency_credit_line_is_not_body_text() -> None:
    """Observed live: Special Nest bodies carried "Genrebild från Shutterstock." as prose."""

    fragment = (
        "<div><p>Genrebild från Shutterstock.</p>"
        "<p>Philip Lindersten har tagit fram konkreta råd inför skolstarten.</p></div>"
    )

    assert _blocks(fragment) == (
        "Philip Lindersten har tagit fram konkreta råd inför skolstarten.",
    )


def test_prose_that_discusses_a_stock_agency_survives() -> None:
    fragment = (
        "<div><p>Shutterstock reported quarterly earnings that beat analyst "
        "expectations by a wide margin this week.</p></div>"
    )

    assert len(_blocks(fragment)) == 1


def test_a_paragraph_repeated_across_the_feed_is_boilerplate() -> None:
    """Observed live: Cyber Security News appended the same ad paragraph to most items in
    one fetch. A paragraph recurring verbatim across three articles is furniture, not news."""

    now = datetime(2026, 8, 9, tzinfo=UTC)
    advert = (
        "Prevent incidents due to slow investigations. Power your Tier 1 with threat "
        "intelligence: Integrate TI Lookup in your SOC."
    )
    shared_by_two = "Both reports cite the same advisory published on Wednesday."
    items = []
    for index in range(3):
        prose = " ".join(f"unique-{index}-{word}" for word in range(90)) + "."
        extra = f"<p>{shared_by_two}</p>" if index < 2 else ""
        items.append(
            f"<item><title>Report {index}</title>"
            f"<link>https://publisher.example/report-{index}</link>"
            f"<guid>repeat-{index}</guid>"
            f"<content:encoded><![CDATA[<p>{prose}</p>{extra}<p>{advert}</p>]]>"
            f"</content:encoded></item>"
        )
    feed = (
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>Repeats</title>{''.join(items)}</channel></rss>"
    ).encode()
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (200, "application/rss+xml", feed),
        }
    ) as site:
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="repeats",
                publisher_id="publisher.example",
                title="Repeats",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.FEED,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    assert len(outcome.articles) == 3
    for article in outcome.articles:
        assert article.body is not None
        assert "Integrate TI Lookup" not in article.body
    two_carriers = [
        article
        for article in outcome.articles
        if article.body is not None and shared_by_two in article.body
    ]
    assert len(two_carriers) == 2


def test_an_article_that_is_mostly_boilerplate_is_omitted() -> None:
    """Stripping feed-wide furniture must not leave a stub pretending to be a full article."""

    now = datetime(2026, 8, 9, tzinfo=UTC)
    advert = (
        "Prevent incidents due to slow investigations. Power your Tier 1 with threat "
        "intelligence: Integrate TI Lookup in your SOC. "
    ) * 6
    items = []
    for index in range(3):
        prose = (
            " ".join(f"unique-{index}-{word}" for word in range(90)) + "."
            if index < 2
            else "One lonely sentence."
        )
        items.append(
            f"<item><title>Report {index}</title>"
            f"<link>https://publisher.example/report-{index}</link>"
            f"<guid>stub-{index}</guid>"
            f"<content:encoded><![CDATA[<p>{prose}</p><p>{advert}</p>]]>"
            f"</content:encoded></item>"
        )
    feed = (
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>Stubs</title>{''.join(items)}</channel></rss>"
    ).encode()
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (200, "application/rss+xml", feed),
        }
    ) as site:
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="stubs",
                publisher_id="publisher.example",
                title="Stubs",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.FEED,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    assert outcome.code == "SOURCE_PARTIAL"
    assert [article.title for article in outcome.articles] == ["Report 0", "Report 1"]
    assert outcome.omitted == 1


def test_a_sponsored_byline_is_not_an_article() -> None:
    """Observed live: a 1,700-word advertorial with byline "Sponsored by Material Security"
    was selected as a real article. Sponsored content is advertising, not journalism."""

    now = datetime(2026, 8, 9, tzinfo=UTC)
    prose = " ".join(f"word-{index}" for index in range(90)) + "."
    feed = f"""<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"
    xmlns:dc="http://purl.org/dc/elements/1.1/">
    <channel><title>Sponsored</title><item><title>The Modern Attack Chain</title>
    <link>https://publisher.example/sponsored</link><guid>sponsored-1</guid>
    <dc:creator>Sponsored by Material Security</dc:creator>
    <content:encoded><![CDATA[<p>{prose}</p>]]></content:encoded></item>
    <item><title>A real report</title>
    <link>https://publisher.example/real</link><guid>real-1</guid>
    <dc:creator>A. Reporter</dc:creator>
    <content:encoded><![CDATA[<p>{prose}</p>]]></content:encoded></item>
    </channel></rss>""".encode()
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (200, "application/rss+xml", feed),
        }
    ) as site:
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="sponsored",
                publisher_id="publisher.example",
                title="Sponsored",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.FEED,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    assert outcome.code == "SOURCE_PARTIAL"
    assert [article.title for article in outcome.articles] == ["A real report"]
    assert outcome.omitted == 1


def test_a_body_cut_mid_sentence_is_demoted_to_a_teaser_link() -> None:
    """Observed live: a Special Nest page under 200 words ended "...har Philip Lindersten,
    som ar grundare av och verksamh" - cut mid-word. It cleared the 80-word full-body minimum
    and was delivered as a complete Article, spending an Article Slot on a paywalled stub."""

    now = datetime(2026, 8, 9, tzinfo=UTC)
    teaser = " ".join(f"word-{index}" for index in range(120))
    feed = f"""<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
    <channel><title>Special Nest</title><item><title>Paywalled report</title>
    <link>https://publisher.example/paywalled</link><guid>teaser-1</guid>
    <content:encoded><![CDATA[<p>{teaser}</p>]]></content:encoded></item></channel></rss>""".encode()
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (200, "application/rss+xml", feed),
        }
    ) as site:
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="teaser",
                publisher_id="publisher.example",
                title="Special Nest",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.FEED,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    assert outcome.articles[0].classification == "teaser_link"
    assert outcome.articles[0].body is None
    assert outcome.articles[0].blocks == ()
    assert outcome.articles[0].title == "Paywalled report"


def test_a_body_ending_with_a_full_stop_stays_a_full_article() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    complete = " ".join(f"word-{index}" for index in range(149)) + "."
    feed = f"""<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
    <channel><title>Complete</title><item><title>Complete report</title>
    <link>https://publisher.example/complete</link><guid>complete-1</guid>
    <content:encoded><![CDATA[<p>{complete}</p>]]></content:encoded></item></channel></rss>""".encode()
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (200, "application/rss+xml", feed),
        }
    ) as site:
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="complete",
                publisher_id="publisher.example",
                title="Complete",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.FEED,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    assert outcome.articles[0].classification == "verified_feed_body"
    assert outcome.articles[0].body == complete


def test_a_mid_sentence_cut_from_a_source_that_allows_short_bodies_stays_short_as_published() -> (
    None
):
    """allow_short_as_published means the operator already accepted short items from this
    Source; the teaser rule must not override that explicit acceptance."""

    now = datetime(2026, 8, 9, tzinfo=UTC)
    stub = " ".join(f"word-{index}" for index in range(30))
    feed = f"""<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
    <channel><title>Short</title><item><title>Short report</title>
    <link>https://publisher.example/short</link><guid>short-1</guid>
    <content:encoded><![CDATA[<p>{stub}</p>]]></content:encoded></item></channel></rss>""".encode()
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (200, "application/rss+xml", feed),
        }
    ) as site:
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="short",
                publisher_id="publisher.example",
                title="Short",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.FEED,
                llm_processing="local_only",
                evidence=evidence(now),
                allow_short_as_published=True,
            )
        )

    assert outcome.articles[0].classification == "short_as_published"
    assert outcome.articles[0].body == stub


def test_a_body_ending_in_a_how_to_list_is_never_demoted_as_a_teaser() -> None:
    """A list or code block is a shape rendering already decided survives; a mid-sentence
    cut only happens to prose, so a body that ends in a list must not be demoted."""

    now = datetime(2026, 8, 9, tzinfo=UTC)
    prose = " ".join(f"word-{index}" for index in range(90)) + "."
    feed = f"""<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
    <channel><title>How-to</title><item><title>How-to report</title>
    <link>https://publisher.example/how-to</link><guid>how-to-1</guid>
    <content:encoded><![CDATA[
      <p>{prose}</p>
      <ol><li>Do the first step of the routine.</li>
      <li>Do the second step of the routine.</li></ol>
    ]]></content:encoded></item></channel></rss>""".encode()
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (200, "application/rss+xml", feed),
        }
    ) as site:
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="how-to",
                publisher_id="publisher.example",
                title="How-to",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.FEED,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    assert outcome.articles[0].classification == "verified_feed_body"
    assert outcome.articles[0].blocks[-1].kind == "list"


def test_an_auto_feed_teaser_falls_back_to_the_complete_page() -> None:
    """A teaser-shaped AUTO feed body must reject the candidate and try the page, exactly
    as the explicit read-full-article CTA does — the complete article is one fetch away."""

    now = datetime(2026, 8, 9, tzinfo=UTC)
    teaser = " ".join(f"stub-{index}" for index in range(100))
    complete = " ".join(f"complete-{index}" for index in range(300)) + "."
    feed = f"""<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
    <channel><title>Auto</title><item><title>Cut in the feed</title>
    <link>REPLACE/reports/cut</link><guid>auto-cut-1</guid>
    <content:encoded><![CDATA[<p>{teaser}</p>]]></content:encoded></item></channel></rss>"""
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (200, "application/rss+xml", b""),
            "/reports/cut": (
                200,
                "text/html",
                f"<html><body><article><p>{complete}</p></article></body></html>".encode(),
            ),
        }
    ) as site:
        site.routes["/feed.xml"] = (
            200,
            "application/rss+xml",
            feed.replace("REPLACE", site.base_url).encode(),
        )
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="auto-cut",
                publisher_id="publisher",
                title="Auto",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.AUTO,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    (article,) = outcome.articles
    assert article.classification == "verified_page_body"
    assert article.body is not None and "complete-5" in article.body


def test_a_repeated_unpunctuated_footer_does_not_demote_complete_articles() -> None:
    """Teaser demotion must run after feed-wide boilerplate stripping: a footer repeated
    across the fetch is furniture, and furniture must not make three complete articles
    read as mid-sentence stubs."""

    now = datetime(2026, 8, 9, tzinfo=UTC)
    items = []
    for index in range(3):
        prose = " ".join(f"complete-{index}-{word}" for word in range(90)) + "."
        items.append(
            f"<item><title>Report {index}</title>"
            f"<link>https://publisher.example/report-{index}</link>"
            f"<guid>footer-{index}</guid>"
            f"<content:encoded><![CDATA[<p>{prose}</p>"
            f"<p>Continue reading with an unpunctuated membership pitch</p>]]>"
            f"</content:encoded></item>"
        )
    feed = (
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>Footers</title>{''.join(items)}</channel></rss>"
    ).encode()
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (200, "application/rss+xml", feed),
        }
    ) as site:
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="footers",
                publisher_id="publisher.example",
                title="Footers",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.FEED,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    assert len(outcome.articles) == 3
    for article in outcome.articles:
        assert article.classification == "verified_feed_body"
        assert article.body is not None
        assert "membership pitch" not in article.body


def test_a_repeated_punctuated_footer_cannot_lend_a_stub_its_full_stop() -> None:
    """The same ordering in the other direction: a repeated punctuated footer is stripped
    first, so the mid-sentence stub it was hiding is still recognized and demoted."""

    now = datetime(2026, 8, 9, tzinfo=UTC)
    items = []
    for index in range(3):
        stub = " ".join(f"stub-{index}-{word}" for word in range(90))
        items.append(
            f"<item><title>Stub {index}</title>"
            f"<link>https://publisher.example/stub-{index}</link>"
            f"<guid>hidden-{index}</guid>"
            f"<content:encoded><![CDATA[<p>{stub}</p>"
            f"<p>This shared legal notice ends with a period.</p>]]>"
            f"</content:encoded></item>"
        )
    feed = (
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>Hidden</title>{''.join(items)}</channel></rss>"
    ).encode()
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (200, "application/rss+xml", feed),
        }
    ) as site:
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="hidden",
                publisher_id="publisher.example",
                title="Hidden",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.FEED,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    assert len(outcome.articles) == 3
    for article in outcome.articles:
        assert article.classification == "teaser_link"
        assert article.body is None


def test_terminal_punctuation_is_not_latin_only() -> None:
    """A complete sentence in another script must not read as a cut: "。", "؟" and "।" end
    sentences exactly as "." does."""

    from epub_news_feeder.acquisition import BodyBlock, _is_teaser

    for ending in ("。", "؟", "।"):
        blocks = (BodyBlock("paragraph", f"a complete sentence in another script{ending}"),)
        assert not _is_teaser(blocks, 90), f"{ending!r} ends a sentence"
    cut = (BodyBlock("paragraph", "a sentence cut mid"),)
    assert _is_teaser(cut, 90)


def test_a_teaser_and_boilerplate_stripping_do_not_collide() -> None:
    """_strip_feedwide_boilerplate runs after every article is classified, including a
    teaser demoted to body=None; it must skip that article rather than treat its empty
    block set as ordinary furniture-free content."""

    now = datetime(2026, 8, 9, tzinfo=UTC)
    advert = (
        "Prevent incidents due to slow investigations. Power your Tier 1 with threat "
        "intelligence: Integrate TI Lookup in your SOC."
    )
    teaser = " ".join(f"word-{index}" for index in range(120))
    items = []
    for index in range(3):
        prose = " ".join(f"unique-{index}-{word}" for word in range(90)) + "."
        items.append(
            f"<item><title>Report {index}</title>"
            f"<link>https://publisher.example/report-{index}</link>"
            f"<guid>collide-{index}</guid>"
            f"<content:encoded><![CDATA[<p>{prose}</p><p>{advert}</p>]]></content:encoded>"
            f"</item>"
        )
    items.append(
        f"<item><title>Paywalled report</title>"
        f"<link>https://publisher.example/paywalled</link>"
        f"<guid>collide-teaser</guid>"
        f"<content:encoded><![CDATA[<p>{teaser}</p>]]></content:encoded></item>"
    )
    feed = (
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>Collide</title>{''.join(items)}</channel></rss>"
    ).encode()
    with fixture_site(
        {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
            "/feed.xml": (200, "application/rss+xml", feed),
        }
    ) as site:
        outcome = SourceClient(now=lambda: now).acquire(
            SourceRequest(
                source_id="collide",
                publisher_id="publisher.example",
                title="Collide",
                feed_url=f"{site.base_url}/feed.xml",
                mode=AcquisitionMode.FEED,
                llm_processing="local_only",
                evidence=evidence(now),
            )
        )

    teaser_articles = [
        article for article in outcome.articles if article.title == "Paywalled report"
    ]
    assert len(teaser_articles) == 1
    assert teaser_articles[0].classification == "teaser_link"
    assert teaser_articles[0].body is None
    for article in outcome.articles:
        if article.title != "Paywalled report":
            assert article.body is not None
            assert "Integrate TI Lookup" not in article.body
