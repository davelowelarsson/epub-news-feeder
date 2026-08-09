from __future__ import annotations

import gzip
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from epub_news_feeder.acquisition import (
    AcquisitionMode,
    EligibilityEvidence,
    SourceClient,
    SourceRequest,
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
    full_text = " ".join(f"verified-word-{index}" for index in range(160))
    page_text = " ".join(f"page-word-{index}" for index in range(160))
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
    second = " ".join(f"complete-second-{index}" for index in range(80))
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
    second = " ".join(f"page-second-{index}" for index in range(80))
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
