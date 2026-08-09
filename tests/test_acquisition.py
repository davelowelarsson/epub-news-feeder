from __future__ import annotations

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
