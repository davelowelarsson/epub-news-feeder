from __future__ import annotations

import re
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from ipaddress import ip_address
from typing import Any, cast
from urllib.parse import urlsplit

import feedparser
import httpcore
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
    default_article_language: str | None = None
    allowed_publisher_origins: tuple[str, ...] = ()
    minimum_full_words: int = 80
    allow_short_as_published: bool = False


@dataclass(frozen=True, slots=True)
class BodyBlock:
    """One classified unit of publisher body text; rendering decides its treatment."""

    kind: str
    text: str


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
    language: str | None
    body: str | None
    blocks: tuple[BodyBlock, ...]
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
                anchored = pattern.endswith("$")
                value = pattern[:-1] if anchored else pattern
                expression = re.escape(value).replace(r"\*", ".*")
                if anchored:
                    expression = f"{expression}$"
                if re.match(expression, path):
                    specificity = len(value.replace("*", ""))
                    matching.append((specificity, directive == "allow"))
        if not matching:
            return True
        longest = max(length for length, _ in matching)
        return any(allowed for length, allowed in matching if length == longest)


class _RobotsParseError(Exception):
    pass


def _parse_robots(body: str) -> _RobotsRules:
    groups: list[tuple[tuple[str, ...], tuple[tuple[str, str], ...]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    saw_rules = False
    for raw_line in body.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            raise _RobotsParseError
        name, value = (part.strip() for part in line.split(":", 1))
        directive = name.casefold()
        if directive == "user-agent":
            if not value:
                raise _RobotsParseError
            if saw_rules and agents:
                groups.append((tuple(agents), tuple(rules)))
                agents, rules, saw_rules = [], [], False
            agents.append(value.casefold())
        elif directive in {"allow", "disallow"}:
            if not agents:
                raise _RobotsParseError
            rules.append((directive, value))
            saw_rules = True
    if agents:
        groups.append((tuple(agents), tuple(rules)))
    return _RobotsRules(tuple(groups))


_DIAGRAM_PREFIXES = (
    "flowchart ",
    "graph ",
    "sequencediagram",
    "classdiagram",
    "statediagram",
    "erdiagram",
    "journey ",
    "gantt ",
    "pie ",
)


def _block_kind(element: HtmlElement, text: str) -> str:
    if element.tag == "blockquote":
        return "quote"
    if element.tag == "li":
        return "list"
    if element.tag == "pre":
        if element.xpath("./code") and text.casefold().startswith(_DIAGRAM_PREFIXES):
            return "diagram"
        return "code"
    return "paragraph"


def _root_blocks(root: HtmlElement, *, fallback_to_root_text: bool) -> tuple[BodyBlock, ...]:
    unwanted = cast(
        list[HtmlElement], root.xpath(".//script|.//style|.//nav|.//footer|.//aside|.//form")
    )
    for element in unwanted:
        element.drop_tree()
    terminal_controls = cast(
        list[HtmlElement],
        root.xpath(".//p[count(*) = 1 and a and not(normalize-space(text())) and not(a/*)]"),
    )
    for paragraph in terminal_controls:
        if " ".join(paragraph.text_content().split()).casefold() in {
            "read full article",
            "comments",
        }:
            paragraph.drop_tree()
    elements = cast(
        list[HtmlElement],
        root.xpath(".//p | .//blockquote | .//li[not(.//p)] | .//pre"),
    )
    blocks: list[BodyBlock] = []
    for element in elements:
        text = " ".join(element.text_content().split())
        if text:
            blocks.append(BodyBlock(_block_kind(element, text), text))
    if blocks or not fallback_to_root_text:
        return tuple(blocks)
    fallback = " ".join(root.text_content().split())
    return (BodyBlock("paragraph", fallback),) if fallback else ()


def _html_blocks(fragment: str) -> tuple[BodyBlock, ...]:
    try:
        root = html.fragment_fromstring(fragment, create_parent="div")
    except (ValueError, TypeError):
        return ()
    return _root_blocks(root, fallback_to_root_text=True)


def _body_text(blocks: tuple[BodyBlock, ...]) -> str:
    return "\n\n".join(block.text for block in blocks if block.kind != "diagram")


def _has_full_article_cta(fragment: str) -> bool:
    try:
        root = html.fragment_fromstring(fragment, create_parent="div")
    except (ValueError, TypeError):
        return False
    paragraphs = cast(list[HtmlElement], root.xpath(".//p[position() > last() - 3]"))
    return any(
        len(paragraph.xpath("./a")) == 1
        and " ".join(paragraph.text_content().split()).casefold() == "read full article"
        for paragraph in paragraphs
    )


def _decoded_feed(payload: bytes) -> str | bytes:
    """Decode a feed as UTF-8, degrading one bad byte rather than the whole document.

    Feeds in the wild are not always the encoding they declare. Danstidningen's, for
    instance, declares UTF-8 and is UTF-8 apart from a stray latin-1 byte, and a parser
    left to sniff the payload gives up on UTF-8 and re-reads *every* character through a
    guessed single-byte codepage — so one broken byte turns every "å" and "ö" in the
    article into mojibake.

    Decoding strictly first keeps the correct 99% correct; only the genuinely undecodable
    bytes become replacement characters. Payloads that are honestly some other encoding
    still reach the parser as bytes, so its detection is preserved for them.
    """

    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    replaced = payload.decode("utf-8-sig", errors="replace")
    # Only rescue a payload that is overwhelmingly UTF-8 already. A genuine latin-1 or
    # cp1252 feed would be riddled with replacements, and belongs with the parser's own
    # charset detection instead.
    if replaced.count("\ufffd") <= max(4, len(replaced) // 2000):
        return replaced
    return payload


def _page_content(
    document: bytes, base_url: httpx.URL
) -> tuple[str, tuple[BodyBlock, ...], str | None]:
    root = cast(HtmlElement, html.fromstring(document))
    canonical_values = cast(
        list[str],
        root.xpath(
            "//link[contains(concat(' ', normalize-space(translate(@rel, "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')), ' '), "
            "' canonical ')]/@href"
        ),
    )
    canonical_url = None
    if canonical_values and canonical_values[0].strip():
        canonical_url = str(base_url.join(canonical_values[0].strip()))
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
    blocks = _root_blocks(containers[0], fallback_to_root_text=False)
    return _body_text(blocks), blocks, canonical_url


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


def _article_language(value: object, fallback: str | None) -> str | None:
    declared = str(value).strip() if value is not None else ""
    if re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", declared):
        return declared
    return fallback


class _RouteDenied(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


type _Origin = tuple[str, str, int]


def _origin(url: httpx.URL) -> _Origin:
    default_port = 443 if url.scheme == "https" else 80
    return url.scheme, url.host.casefold(), url.port or default_port


def _is_loopback(url: httpx.URL) -> bool:
    try:
        return ip_address(url.host).is_loopback
    except ValueError:
        return url.host.casefold() == "localhost" or url.host.casefold().endswith(".localhost")


def _is_non_public_address(url: httpx.URL) -> bool:
    try:
        return not ip_address(url.host).is_global
    except ValueError:
        hostname = url.host.casefold().rstrip(".")
        return hostname == "localhost" or hostname.endswith((".localhost", ".local"))


def _parse_safe_url(value: str, *, loopback_origin: _Origin | None = None) -> httpx.URL:
    try:
        url = httpx.URL(value)
    except (httpx.InvalidURL, ValueError) as error:
        raise _RouteDenied("SOURCE_URL_UNSAFE") from error
    if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
        raise _RouteDenied("SOURCE_URL_UNSAFE")
    if _is_non_public_address(url) and not (
        _is_loopback(url) and loopback_origin is not None and _origin(url) == loopback_origin
    ):
        raise _RouteDenied("SOURCE_URL_UNSAFE")
    return url


def _feed_url_context(value: str) -> tuple[httpx.URL, _Origin, _Origin | None]:
    try:
        raw_url = httpx.URL(value)
    except (httpx.InvalidURL, ValueError) as error:
        raise _RouteDenied("SOURCE_URL_UNSAFE") from error
    raw_origin = _origin(raw_url)
    loopback_origin = raw_origin if _is_loopback(raw_url) else None
    return _parse_safe_url(value, loopback_origin=loopback_origin), raw_origin, loopback_origin


def _require_public_resolution(url: httpx.URL, loopback_origin: _Origin | None) -> None:
    if loopback_origin is not None and _origin(url) == loopback_origin and _is_loopback(url):
        return
    try:
        addresses = socket.getaddrinfo(url.host, url.port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise _RouteDenied("SOURCE_HOST_UNRESOLVED") from error
    if not addresses:
        raise _RouteDenied("SOURCE_HOST_UNRESOLVED")
    for address in addresses:
        resolved = str(address[4][0]).split("%", 1)[0]
        try:
            if not ip_address(resolved).is_global:
                raise _RouteDenied("SOURCE_URL_UNSAFE")
        except ValueError as error:
            raise _RouteDenied("SOURCE_HOST_UNRESOLVED") from error


class _PinnedNetworkBackend(httpcore.SyncBackend):
    """Resolve once, validate, then connect to that exact address."""

    def __init__(self) -> None:
        self._loopback_origins: set[tuple[str, int]] = set()

    def allow_loopback(self, origin: _Origin | None) -> None:
        if origin is not None:
            self._loopback_origins.add((origin[1], origin[2]))

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as error:
            raise httpcore.ConnectError("Host resolution failed") from error
        resolved: list[str] = []
        for address_info in addresses:
            value = str(address_info[4][0]).split("%", 1)[0]
            try:
                parsed = ip_address(value)
            except ValueError as error:
                raise httpcore.ConnectError("Host resolution was invalid") from error
            if not parsed.is_global and not (
                parsed.is_loopback and (host.casefold(), port) in self._loopback_origins
            ):
                raise httpcore.ConnectError("Host resolution was not public")
            if value not in resolved:
                resolved.append(value)
        if not resolved:
            raise httpcore.ConnectError("Host resolution was empty")
        last_error: httpcore.ConnectError | None = None
        for resolved_address in resolved:
            try:
                return super().connect_tcp(
                    resolved_address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.ConnectError as error:
                last_error = error
        assert last_error is not None
        raise last_error


class _PinnedHTTPTransport(httpx.HTTPTransport):
    def __init__(self, backend: _PinnedNetworkBackend) -> None:
        super().__init__(trust_env=False)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(network_backend=backend)


class SourceClient:
    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        user_agent: str = "epub-news-feeder/0.1 (+https://github.com/davelowelarsson/epub-news-feeder)",
        timeout: float = 20,
        max_attempts: int = 3,
        max_response_bytes: int = 5 * 1024 * 1024,
        max_article_body_bytes: int = 1024 * 1024,
    ) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._product = "epub-news-feeder"
        self._network_backend = _PinnedNetworkBackend()
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=_PinnedHTTPTransport(self._network_backend),
        )
        self._max_attempts = max_attempts
        self._max_response_bytes = max_response_bytes
        self._max_article_body_bytes = max_article_body_bytes
        self._robots: dict[str, _RobotsRules | str] = {}

    def close(self) -> None:
        self._client.close()

    def acquire(self, request: SourceRequest) -> AcquisitionOutcome:
        denied = self._evidence_denial(request)
        if denied is not None:
            return AcquisitionOutcome(request.source_id, denied, ())
        try:
            feed_url, feed_origin, loopback_origin = _feed_url_context(request.feed_url)
            feed = self._get_permitted(
                feed_url,
                allowed_origins={feed_origin},
                loopback_origin=loopback_origin,
            )
        except _RouteDenied as error:
            return AcquisitionOutcome(request.source_id, error.code, ())

        parsed: Any = feedparser.parse(_decoded_feed(feed.content))
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
        try:
            _, feed_origin, loopback_origin = _feed_url_context(request.feed_url)
            link_url = _parse_safe_url(link, loopback_origin=loopback_origin)
            publisher_origins = self._publisher_origins(request, feed_origin, loopback_origin)
        except _RouteDenied:
            return None
        if (
            request.mode
            in {
                AcquisitionMode.WEB,
                AcquisitionMode.AUTO,
                AcquisitionMode.METADATA_ONLY,
            }
            and _origin(link_url) not in publisher_origins
        ):
            return None
        if request.mode in {
            AcquisitionMode.WEB,
            AcquisitionMode.AUTO,
            AcquisitionMode.METADATA_ONLY,
        }:
            try:
                _require_public_resolution(link_url, loopback_origin)
            except _RouteDenied:
                return None
        guid_value = entry.get("id") or entry.get("guid")
        guid = str(guid_value) if guid_value is not None else None
        author_value = entry.get("author")
        author = str(author_value).strip() if author_value else None
        published = _entry_datetime(entry.get("published") or entry.get("updated"))
        language = _article_language(entry.get("language"), request.default_article_language)
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
                str(link_url),
                published,
                categories,
                language,
                None,
                (),
                "metadata_only",
            )

        body: str | None = None
        blocks: tuple[BodyBlock, ...] = ()
        classification: str | None = None
        content = entry.get("content")
        if request.mode in {AcquisitionMode.FEED, AcquisitionMode.AUTO} and isinstance(
            content, list
        ):
            for item in content:
                if isinstance(item, Mapping):
                    fragment = str(item.get("value", ""))
                    candidate_blocks = _html_blocks(fragment)
                    candidate = _body_text(candidate_blocks)
                    if request.mode == AcquisitionMode.AUTO and _has_full_article_cta(fragment):
                        continue
                    if len(candidate.encode("utf-8")) > self._max_article_body_bytes:
                        continue
                    if len(candidate.split()) >= request.minimum_full_words:
                        body, blocks, classification = (
                            candidate,
                            candidate_blocks,
                            "verified_feed_body",
                        )
                        break
                    if candidate and request.allow_short_as_published:
                        body, blocks, classification = (
                            candidate,
                            candidate_blocks,
                            "short_as_published",
                        )
                        break
        if body is None and request.mode in {AcquisitionMode.WEB, AcquisitionMode.AUTO}:
            try:
                response = self._get_permitted(
                    link_url,
                    allowed_origins=publisher_origins,
                    loopback_origin=loopback_origin,
                )
            except _RouteDenied:
                return None
            candidate, candidate_blocks, canonical = _page_content(response.content, response.url)
            if canonical is not None:
                try:
                    canonical_url = _parse_safe_url(canonical, loopback_origin=loopback_origin)
                except _RouteDenied:
                    return None
                if _origin(canonical_url) not in publisher_origins:
                    return None
                try:
                    _require_public_resolution(canonical_url, loopback_origin)
                except _RouteDenied:
                    return None
                link_url = canonical_url
            else:
                link_url = response.url
            if len(candidate.encode("utf-8")) > self._max_article_body_bytes:
                return None
            if len(candidate.split()) >= request.minimum_full_words:
                body, blocks, classification = candidate, candidate_blocks, "verified_page_body"
            elif candidate and request.allow_short_as_published:
                body, blocks, classification = candidate, candidate_blocks, "short_as_published"
        if body is None or classification is None:
            return None
        return AcquiredArticle(
            request.source_id,
            request.publisher_id,
            request.title,
            guid,
            title,
            author,
            str(link_url),
            published,
            categories,
            language,
            body,
            blocks,
            classification,
        )

    def _publisher_origins(
        self,
        request: SourceRequest,
        feed_origin: _Origin,
        loopback_origin: _Origin | None,
    ) -> set[_Origin]:
        if request.allowed_publisher_origins:
            values = request.allowed_publisher_origins
        else:
            inferred = [f"{feed_origin[0]}://{feed_origin[1]}:{feed_origin[2]}"]
            publisher = request.publisher_id.casefold().rstrip(".")
            if re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", publisher) and "." in publisher:
                inferred.extend((f"https://{publisher}", f"https://www.{publisher}"))
            values = tuple(inferred)
        origins: set[_Origin] = set()
        for value in values:
            url = _parse_safe_url(value, loopback_origin=loopback_origin)
            origins.add(_origin(url))
        return origins

    def _get_permitted(
        self,
        url: str | httpx.URL,
        *,
        allowed_origins: set[_Origin],
        loopback_origin: _Origin | None,
    ) -> httpx.Response:
        current = _parse_safe_url(str(url), loopback_origin=loopback_origin)
        self._network_backend.allow_loopback(loopback_origin)
        for _ in range(6):
            if _origin(current) not in allowed_origins:
                raise _RouteDenied("SOURCE_ORIGIN_NOT_ALLOWED")
            _require_public_resolution(current, loopback_origin)
            self._require_robots(current, loopback_origin)
            response = self._request(current, loopback_origin)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise _RouteDenied("SOURCE_REDIRECT_INVALID")
                current = _parse_safe_url(
                    str(current.join(location)), loopback_origin=loopback_origin
                )
                continue
            if response.status_code in {401, 403, 451}:
                raise _RouteDenied("SOURCE_ACCESS_CONTROLLED")
            if response.status_code >= 400:
                raise _RouteDenied("SOURCE_FETCH_FAILED")
            return response
        raise _RouteDenied("SOURCE_REDIRECT_LIMIT")

    def _request(self, url: httpx.URL, loopback_origin: _Origin | None) -> httpx.Response:
        last_response: httpx.Response | None = None
        for _ in range(self._max_attempts):
            try:
                response = self._download(url, loopback_origin)
            except httpx.TransportError:
                continue
            last_response = response
            if response.status_code not in {408, 429} and response.status_code < 500:
                return response
        if last_response is not None:
            return last_response
        raise _RouteDenied("SOURCE_TRANSPORT_FAILED")

    def _download(self, url: httpx.URL, loopback_origin: _Origin | None) -> httpx.Response:
        with self._client.stream("GET", url) as response:
            stream = response.extensions.get("network_stream")
            if stream is None:
                raise _RouteDenied("SOURCE_PEER_UNVERIFIED")
            peer = stream.get_extra_info("server_addr")
            if not (isinstance(peer, tuple) and peer and isinstance(peer[0], str)):
                raise _RouteDenied("SOURCE_PEER_UNVERIFIED")
            resolved = peer[0].split("%", 1)[0]
            try:
                address = ip_address(resolved)
            except ValueError as error:
                raise _RouteDenied("SOURCE_HOST_UNRESOLVED") from error
            loopback_allowed = (
                address.is_loopback
                and loopback_origin is not None
                and _origin(url) == loopback_origin
            )
            if not address.is_global and not loopback_allowed:
                raise _RouteDenied("SOURCE_URL_UNSAFE")
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    declared_size = 0
                if declared_size > self._max_response_bytes:
                    raise _RouteDenied("SOURCE_RESPONSE_TOO_LARGE")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > self._max_response_bytes:
                    raise _RouteDenied("SOURCE_RESPONSE_TOO_LARGE")
            return httpx.Response(
                response.status_code,
                headers={
                    name: value
                    for name, value in response.headers.items()
                    if name.casefold()
                    not in {"content-encoding", "content-length", "transfer-encoding"}
                },
                content=bytes(content),
                request=response.request,
            )

    def _require_robots(self, url: httpx.URL, loopback_origin: _Origin | None) -> None:
        origin = f"{url.scheme}://{url.host}"
        if url.port is not None:
            origin = f"{origin}:{url.port}"
        cached = self._robots.get(origin)
        if cached is None:
            robots_url = httpx.URL(f"{origin}/robots.txt")
            try:
                response = self._request(robots_url, loopback_origin)
            except _RouteDenied:
                self._robots[origin] = "SOURCE_ROBOTS_UNAVAILABLE"
            else:
                if response.status_code == 404:
                    self._robots[origin] = _RobotsRules(())
                elif response.status_code in {301, 302, 303, 307, 308}:
                    self._robots[origin] = "SOURCE_ROBOTS_UNAVAILABLE"
                elif response.status_code in {401, 403, 451}:
                    self._robots[origin] = "SOURCE_ACCESS_CONTROLLED"
                elif response.status_code >= 400:
                    self._robots[origin] = "SOURCE_ROBOTS_UNAVAILABLE"
                else:
                    try:
                        body = response.content.decode("utf-8-sig", errors="strict")
                        self._robots[origin] = _parse_robots(body)
                    except (UnicodeDecodeError, _RobotsParseError):
                        self._robots[origin] = "SOURCE_ROBOTS_INVALID"
            cached = self._robots[origin]
        if isinstance(cached, str):
            raise _RouteDenied(cached)
        parsed = urlsplit(str(url))
        path_and_query = parsed.path or "/"
        if parsed.query:
            path_and_query = f"{path_and_query}?{parsed.query}"
        if not cached.allows(self._product, path_and_query):
            raise _RouteDenied("SOURCE_ROBOTS_DENIED")
