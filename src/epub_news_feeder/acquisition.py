from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, cast
from urllib.parse import urlsplit

import feedparser
import httpx
from lxml import html
from lxml.html import HtmlElement


class AcquisitionMode(StrEnum):
    FEED = "feed"
    WEB = "web"
    AUTO = "auto"
    METADATA_ONLY = "metadata_only"


@dataclass(frozen=True, slots=True)
class EligibilityEvidence:
    evidence_id: str
    reviewed_at: datetime
    expires_at: datetime
    feed_acquisition: str
    page_acquisition: str
    retention: str
    private_distribution: str
    local_llm: str
    remote_llm: str


@dataclass(frozen=True, slots=True)
class SourceRequest:
    source_id: str
    publisher_id: str
    title: str
    feed_url: str
    mode: AcquisitionMode
    llm_processing: str
    evidence: EligibilityEvidence
    minimum_full_words: int = 80
    allow_short_as_published: bool = False


@dataclass(frozen=True, slots=True)
class AcquiredArticle:
    source_id: str
    publisher_id: str
    source_title: str
    guid: str | None
    title: str
    author: str | None
    canonical_url: str
    published_at: datetime | None
    categories: tuple[str, ...]
    body: str | None
    classification: str


@dataclass(frozen=True, slots=True)
class AcquisitionOutcome:
    source_id: str
    code: str
    articles: tuple[AcquiredArticle, ...]
    omitted: int = 0


@dataclass(frozen=True, slots=True)
class _RobotsRules:
    groups: tuple[tuple[tuple[str, ...], tuple[tuple[str, str], ...]], ...]

    def allows(self, product: str, path: str) -> bool:
        exact = [rules for agents, rules in self.groups if product.casefold() in agents]
        selected = exact or [rules for agents, rules in self.groups if "*" in agents]
        matching: list[tuple[int, bool]] = []
        for rules in selected:
            for directive, pattern in rules:
                if not pattern:
                    continue
                normalized = pattern.split("*", 1)[0]
                if path.startswith(normalized):
                    matching.append((len(normalized), directive == "allow"))
        if not matching:
            return True
        longest = max(length for length, _ in matching)
        return any(allowed for length, allowed in matching if length == longest)


def _parse_robots(body: str) -> _RobotsRules:
    groups: list[tuple[tuple[str, ...], tuple[tuple[str, str], ...]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    saw_rules = False
    for raw_line in body.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        name, value = (part.strip() for part in line.split(":", 1))
        directive = name.casefold()
        if directive == "user-agent":
            if saw_rules and agents:
                groups.append((tuple(agents), tuple(rules)))
                agents, rules, saw_rules = [], [], False
            agents.append(value.casefold())
        elif directive in {"allow", "disallow"} and agents:
            rules.append((directive, value))
            saw_rules = True
    if agents:
        groups.append((tuple(agents), tuple(rules)))
    return _RobotsRules(tuple(groups))


def _html_text(fragment: str) -> str:
    try:
        root = html.fragment_fromstring(fragment, create_parent="div")
    except (ValueError, TypeError):
        return ""
    return " ".join(root.text_content().split())


def _page_text(document: bytes) -> str:
    root = cast(HtmlElement, html.fromstring(document))
    unwanted_elements = cast(
        list[HtmlElement], root.xpath("//script|//style|//nav|//footer|//aside")
    )
    for unwanted in unwanted_elements:
        unwanted.drop_tree()
    containers = cast(list[HtmlElement], root.xpath("//article"))
    if not containers:
        containers = cast(list[HtmlElement], root.xpath("//main"))
    if not containers:
        containers = [root]
    paragraphs: list[str] = []
    for container in containers[:1]:
        for paragraph in cast(list[HtmlElement], container.xpath(".//p")):
            value = " ".join(paragraph.text_content().split())
            if value:
                paragraphs.append(value)
    return "\n\n".join(paragraphs)


def _entry_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class _RouteDenied(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SourceClient:
    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        user_agent: str = "epub-news-feeder/0.1 (+https://github.com/davelowelarsson/epub-news-feeder)",
        timeout: float = 20,
        max_attempts: int = 3,
    ) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._product = "epub-news-feeder"
        self._client = httpx.Client(
            headers={"User-Agent": user_agent}, timeout=timeout, follow_redirects=False
        )
        self._max_attempts = max_attempts
        self._robots: dict[str, _RobotsRules | str] = {}

    def close(self) -> None:
        self._client.close()

    def acquire(self, request: SourceRequest) -> AcquisitionOutcome:
        denied = self._evidence_denial(request)
        if denied is not None:
            return AcquisitionOutcome(request.source_id, denied, ())
        try:
            feed = self._get_permitted(request.feed_url)
        except _RouteDenied as error:
            return AcquisitionOutcome(request.source_id, error.code, ())

        parsed: Any = feedparser.parse(feed.content)
        if bool(getattr(parsed, "bozo", False)) and not getattr(parsed, "entries", []):
            return AcquisitionOutcome(request.source_id, "SOURCE_FEED_INVALID", ())

        articles: list[AcquiredArticle] = []
        omitted = 0
        for entry in parsed.entries:
            article = self._entry(request, entry)
            if article is None:
                omitted += 1
            else:
                articles.append(article)
        code = "SOURCE_OK" if omitted == 0 else "SOURCE_PARTIAL"
        return AcquisitionOutcome(request.source_id, code, tuple(articles), omitted)

    def _evidence_denial(self, request: SourceRequest) -> str | None:
        evidence = request.evidence
        if evidence.expires_at.astimezone(UTC) <= self._now().astimezone(UTC):
            return "SOURCE_RIGHTS_REVIEW_EXPIRED"
        if evidence.feed_acquisition != "allow":
            return "SOURCE_FEED_NOT_ALLOWED"
        if request.mode != AcquisitionMode.METADATA_ONLY and (
            evidence.retention != "allow" or evidence.private_distribution != "allow"
        ):
            return "SOURCE_RETENTION_NOT_ALLOWED"
        if (
            request.mode in {AcquisitionMode.WEB, AcquisitionMode.AUTO}
            and evidence.page_acquisition != "allow"
        ):
            return "SOURCE_PAGE_NOT_ALLOWED"
        return None

    def _entry(self, request: SourceRequest, entry: Mapping[str, Any]) -> AcquiredArticle | None:
        title = str(entry.get("title", "")).strip()
        link = str(entry.get("link", "")).strip()
        if not title or not link:
            return None
        guid_value = entry.get("id") or entry.get("guid")
        guid = str(guid_value) if guid_value is not None else None
        author_value = entry.get("author")
        author = str(author_value).strip() if author_value else None
        published = _entry_datetime(entry.get("published") or entry.get("updated"))
        tags: Sequence[Mapping[str, Any]] = entry.get("tags", ())
        categories = tuple(
            str(tag.get("term", "")).strip() for tag in tags if str(tag.get("term", "")).strip()
        )

        if request.mode == AcquisitionMode.METADATA_ONLY:
            return AcquiredArticle(
                request.source_id,
                request.publisher_id,
                request.title,
                guid,
                title,
                author,
                link,
                published,
                categories,
                None,
                "metadata_only",
            )

        body: str | None = None
        classification: str | None = None
        content = entry.get("content")
        if request.mode in {AcquisitionMode.FEED, AcquisitionMode.AUTO} and isinstance(
            content, list
        ):
            for item in content:
                if isinstance(item, Mapping):
                    candidate = _html_text(str(item.get("value", "")))
                    if len(candidate.split()) >= request.minimum_full_words:
                        body, classification = candidate, "verified_feed_body"
                        break
                    if candidate and request.allow_short_as_published:
                        body, classification = candidate, "short_as_published"
                        break
        if body is None and request.mode in {AcquisitionMode.WEB, AcquisitionMode.AUTO}:
            try:
                response = self._get_permitted(link)
            except _RouteDenied:
                return None
            candidate = _page_text(response.content)
            if len(candidate.split()) >= request.minimum_full_words:
                body, classification = candidate, "verified_page_body"
            elif candidate and request.allow_short_as_published:
                body, classification = candidate, "short_as_published"
        if body is None or classification is None:
            return None
        return AcquiredArticle(
            request.source_id,
            request.publisher_id,
            request.title,
            guid,
            title,
            author,
            link,
            published,
            categories,
            body,
            classification,
        )

    def _get_permitted(self, url: str) -> httpx.Response:
        current = httpx.URL(url)
        for _ in range(6):
            self._require_robots(current)
            response = self._request(current)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise _RouteDenied("SOURCE_REDIRECT_INVALID")
                current = current.join(location)
                continue
            if response.status_code in {401, 403, 451}:
                raise _RouteDenied("SOURCE_ACCESS_CONTROLLED")
            if response.status_code >= 400:
                raise _RouteDenied("SOURCE_FETCH_FAILED")
            return response
        raise _RouteDenied("SOURCE_REDIRECT_LIMIT")

    def _request(self, url: httpx.URL) -> httpx.Response:
        last_response: httpx.Response | None = None
        for _ in range(self._max_attempts):
            try:
                response = self._client.get(url)
            except httpx.TransportError:
                continue
            last_response = response
            if response.status_code not in {408, 429} and response.status_code < 500:
                return response
        if last_response is not None:
            return last_response
        raise _RouteDenied("SOURCE_TRANSPORT_FAILED")

    def _require_robots(self, url: httpx.URL) -> None:
        origin = f"{url.scheme}://{url.host}"
        if url.port is not None:
            origin = f"{origin}:{url.port}"
        cached = self._robots.get(origin)
        if cached is None:
            robots_url = httpx.URL(f"{origin}/robots.txt")
            try:
                response = self._request(robots_url)
            except _RouteDenied:
                self._robots[origin] = "SOURCE_ROBOTS_UNAVAILABLE"
            else:
                if response.status_code == 404:
                    self._robots[origin] = _RobotsRules(())
                elif response.status_code in {401, 403, 451}:
                    self._robots[origin] = "SOURCE_ACCESS_CONTROLLED"
                elif response.status_code >= 400:
                    self._robots[origin] = "SOURCE_ROBOTS_UNAVAILABLE"
                else:
                    self._robots[origin] = _parse_robots(response.text)
            cached = self._robots[origin]
        if isinstance(cached, str):
            raise _RouteDenied(cached)
        path = urlsplit(str(url)).path or "/"
        if not cached.allows(self._product, path):
            raise _RouteDenied("SOURCE_ROBOTS_DENIED")
