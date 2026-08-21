from __future__ import annotations

import re
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
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
class _PageBody:
    """One publisher page's extracted body, plus the raw document for metadata reads."""

    body: str
    blocks: tuple[BodyBlock, ...]
    classification: str
    url: httpx.URL
    document: bytes


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
        return _without_furniture(tuple(blocks))
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


_MAXIMUM_LIST_RUN = 12
_SHORT_LIST_ITEM_WORDS = 8

# WordPress appends this to every feed item's content; it is metadata about syndication,
# never publisher prose, so the anchored full match cannot condemn a real sentence.
_WORDPRESS_TRAILER = re.compile(r"^The post .+ appeared first on .+$")

# The longest a navigation menu entry gets. "Om oss", "Logga in", "Prenumeration" are one
# or two words; a real first sentence is not.
_CHROME_LIST_ITEM_WORDS = 4

_CREDIT_LINE_WORDS = 8
_STOCK_AGENCIES = ("shutterstock", "getty images", "istock", "unsplash")
_CREDIT_PREFIXES = ("foto:", "bild:", "photo:", "image:", "genrebild")


def _without_furniture(blocks: tuple[BodyBlock, ...]) -> tuple[BodyBlock, ...]:
    """Drop the kinds of publisher furniture that read as body text but are not.

    Deliberately a list of narrow signatures rather than a general classifier — the general
    question of separating article text from furniture is open (issue #85). Each rule below
    was observed corrupting a delivered Edition and is precise enough to act on:

    - **Fixture tables.** An SVT Sport report ended in a hundred consecutive list items of
      four words each - a whole season's results, one scoreline per item - rendered as
      prose. A long run of uniformly tiny list items is a table, not a list a reader wants.
      A numbered how-to is short, and a genuine list of substantial items has long items,
      so both survive.
    - **Section labels.** A bare ``BLOG`` on its own, which is a heading the page styles
      and a reader cannot place.
    - **Feed trailers.** WordPress closes every item with "The post <title> appeared first
      on <site>." — syndication metadata, not journalism.
    - **Leading navigation.** Special Nest pages open with the site menu — "Om oss",
      "Cookiepolicy", "Logga in" — extracted as a list before the first paragraph. A run of
      uniformly tiny list items ahead of any prose is chrome; after prose it may be content.
    - **Stock-photo credits.** "Genrebild från Shutterstock." is a caption for an image the
      Edition does not carry.
    - **Related-headlines widgets.** Special Nest pages end in a list of other articles'
      headlines. Beyond being junk, the widget changes as the site publishes, so an
      unchanged article kept re-reading as materially updated and re-entering Editions.
    """

    kept: list[BodyBlock] = []
    index = 0
    seen_prose = False
    while index < len(blocks):
        block = blocks[index]
        if block.kind != "list":
            if (
                not _is_bare_label(block)
                and not _is_credit_line(block)
                and not (block.kind == "paragraph" and _WORDPRESS_TRAILER.match(block.text))
            ):
                kept.append(block)
                seen_prose = True
            index += 1
            continue
        run_end = index
        while run_end < len(blocks) and blocks[run_end].kind == "list":
            run_end += 1
        run = blocks[index:run_end]
        if (
            _is_tabular_run(run)
            or (not seen_prose and _is_chrome_run(run))
            or (seen_prose and run_end == len(blocks) and _is_related_headline_run(run))
        ):
            index = run_end
            continue
        kept.extend(item for item in run if not _is_bare_label(item))
        seen_prose = True
        index = run_end
    return tuple(kept)


def _is_chrome_run(run: tuple[BodyBlock, ...] | list[BodyBlock]) -> bool:
    """A list of uniformly tiny items ahead of any prose is a navigation menu."""

    return all(len(item.text.split()) <= _CHROME_LIST_ITEM_WORDS for item in run)


_RELATED_HEADLINE_MIN_ITEMS = 2
_RELATED_HEADLINE_MIN_WORDS = 5
# Closing quotes and brackets a headline may end with, ahead of the punctuation test:
# straight quotes, curly double and single closing quotes, guillemet, parenthesis, bracket.
_CLOSING_MARKS = "\"'\u201d\u2019\u00bb)]"


def _is_related_headline_run(run: tuple[BodyBlock, ...] | list[BodyBlock]) -> bool:
    """A body-final run of headline-shaped items is a related-articles widget.

    Headline-shaped means every item is long enough to be a headline rather than a keyword,
    and no item ends in a full stop. Headlines end with question marks, exclamations and
    quotes but not periods; a how-to list writes sentences and a packing list writes short
    noun phrases, so both survive while a widget of nothing but headlines does not.
    """

    if len(run) < _RELATED_HEADLINE_MIN_ITEMS:
        return False
    for item in run:
        if len(item.text.split()) < _RELATED_HEADLINE_MIN_WORDS:
            return False
        if item.text.rstrip(_CLOSING_MARKS).endswith("."):
            return False
    return True


def _is_credit_line(block: BodyBlock) -> bool:
    """A short standalone line naming a stock agency is an image credit, not a sentence."""

    if block.kind != "paragraph" or len(block.text.split()) > _CREDIT_LINE_WORDS:
        return False
    lowered = block.text.casefold()
    return lowered.startswith(_CREDIT_PREFIXES) or any(
        agency in lowered for agency in _STOCK_AGENCIES
    )


def _is_tabular_run(run: tuple[BodyBlock, ...] | list[BodyBlock]) -> bool:
    """A long run of uniformly tiny list items is a table rendered as a list."""

    if len(run) <= _MAXIMUM_LIST_RUN:
        return False
    lengths = sorted(len(item.text.split()) for item in run)
    median = lengths[len(lengths) // 2]
    return median <= _SHORT_LIST_ITEM_WORDS


def _is_bare_label(block: BodyBlock) -> bool:
    """A single all-capitals token is a styled section heading, not a sentence."""

    text = block.text.strip()
    return len(text) <= 12 and text.isupper() and len(text.split()) == 1


# In how many distinct articles of one fetch a block must recur verbatim before it is
# furniture. Three, not two: two articles legitimately quoting the same advisory sentence
# were observed live, while the ad paragraphs this exists for ran across most of the feed.
_MINIMUM_BOILERPLATE_ARTICLES = 3


def _strip_feedwide_boilerplate(
    articles: list[AcquiredArticle], request: SourceRequest
) -> tuple[list[AcquiredArticle], int]:
    """Drop blocks that recur verbatim across articles of one fetch; they are furniture.

    Observed live: Cyber Security News appended the same marketing paragraph to most items
    in a feed, and Special Nest pages carried identical site chrome into every extraction.
    No sentence of actual journalism repeats verbatim across three different articles, so
    repetition within one fetch is a signature that needs no per-publisher pattern list.

    Bodies are rebuilt from the surviving blocks so revision hashing sees the same text a
    reader does — otherwise shifting furniture keeps manufacturing false "material updates".
    An article reduced below the full-body minimum was mostly furniture and is omitted
    rather than delivered as a stub.
    """

    counts: dict[str, int] = {}
    for article in articles:
        for text in {block.text for block in article.blocks}:
            counts[text] = counts.get(text, 0) + 1
    boilerplate = {text for text, count in counts.items() if count >= _MINIMUM_BOILERPLATE_ARTICLES}
    if not boilerplate:
        return articles, 0

    kept: list[AcquiredArticle] = []
    omitted = 0
    for article in articles:
        if article.body is None or not any(block.text in boilerplate for block in article.blocks):
            kept.append(article)
            continue
        blocks = tuple(block for block in article.blocks if block.text not in boilerplate)
        body = _body_text(blocks)
        if len(body.split()) >= request.minimum_full_words:
            kept.append(replace(article, body=body, blocks=blocks))
        elif body and request.allow_short_as_published:
            kept.append(
                replace(article, body=body, blocks=blocks, classification="short_as_published")
            )
        else:
            omitted += 1
    return kept, omitted


_TEASER_MAXIMUM_WORDS = 200
# A complete sentence ends with one of these; anything else after stripping _CLOSING_MARKS
# is a cut, not an ending.
_TERMINAL_MARKS = ".!?…"


def _is_teaser(blocks: tuple[BodyBlock, ...], word_count: int) -> bool:
    """A short body whose last paragraph stops mid-sentence is a paywall teaser, not an Article.

    Observed live: a Special Nest page under 200 words ended "...har Philip Lindersten, som
    ar grundare av och verksamh" - cut mid-word. It cleared the 80-word full-body minimum and
    was delivered as a complete Article, spending an Article Slot on something that was not
    readable journalism.

    A body ending in a list or code block is exempt: rendering already decided that shape
    survives, and a mid-sentence cut only happens to prose. A headline plus the publisher
    route is an honest description of what the Source's evidence allows; a stub pretending to
    be a finished Article is not.
    """

    if word_count >= _TEASER_MAXIMUM_WORDS or not blocks or blocks[-1].kind != "paragraph":
        return False
    trimmed = blocks[-1].text.rstrip(_CLOSING_MARKS)
    return not trimmed or trimmed[-1] not in _TERMINAL_MARKS


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


def _page_metadata(document: bytes) -> tuple[str | None, datetime | None]:
    """A page's own title and published time, for the one route that has no feed entry.

    ``og:title`` over ``<title>``: publishers keep the former clean while the latter
    usually carries a "| Site" suffix a reader should not see as a headline. The
    published time is optional — an undated recovery is treated as fresh downstream,
    exactly like an undated feed entry.
    """

    root = cast(HtmlElement, html.fromstring(document))
    og_titles = cast(list[str], root.xpath("//meta[@property='og:title']/@content"))
    page_titles = cast(list[str], root.xpath("//title/text()"))
    title = next((value.strip() for value in (*og_titles, *page_titles) if value.strip()), None)
    published_values = cast(
        list[str], root.xpath("//meta[@property='article:published_time']/@content")
    )
    published = _entry_datetime(published_values[0]) if published_values else None
    return title, published


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
        articles, boilerplate_omitted = _strip_feedwide_boilerplate(articles, request)
        omitted += boilerplate_omitted
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
        # A byline that opens with "Sponsored" is the publisher's own label for paid
        # placement. Advertising is not journalism, so it never becomes an Article or a
        # Brief — observed live as a 1,700-word advertorial filling a personal Section.
        if author is not None and author.casefold().startswith("sponsored"):
            return None
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
            page = self._page_body(request, link_url, publisher_origins, loopback_origin)
            if page is None:
                return None
            body, blocks, classification = page.body, page.blocks, page.classification
            link_url = page.url
        if body is None or classification is None:
            return None
        # Applied only once the body route is fully resolved - after the AUTO-mode web
        # fallback, on whatever body won - because a feed teaser that AUTO rejects for a
        # complete page must not be judged on the teaser it never kept.
        if not request.allow_short_as_published and _is_teaser(blocks, len(body.split())):
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
                "teaser_link",
            )
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

    def acquire_article(self, request: SourceRequest, url: str) -> AcquiredArticle | None:
        """Re-acquire one Article by its stored canonical URL, for Near Miss recovery.

        The URL comes from the State Store, which is data rather than authority, so this
        route re-derives and re-checks everything the feed route would have: the Source's
        evidence (including ``page_acquisition``), the publisher origin allowlist, public
        resolution, and — inside ``_page_body`` — robots, redirect discipline, SSRF pinning
        and the size caps. Only ``web`` and ``auto`` Sources qualify: a ``feed`` or
        ``metadata_only`` Source's evidence never contemplated page fetches, so recovery
        must not introduce them.

        With no feed entry to speak for the page, the title and publication time come from
        the page's own metadata; a page that declares no usable title is skipped rather
        than delivered nameless.
        """

        if request.mode not in {AcquisitionMode.WEB, AcquisitionMode.AUTO}:
            return None
        if self._evidence_denial(request) is not None:
            return None
        try:
            _, feed_origin, loopback_origin = _feed_url_context(request.feed_url)
            link_url = _parse_safe_url(url, loopback_origin=loopback_origin)
            publisher_origins = self._publisher_origins(request, feed_origin, loopback_origin)
        except _RouteDenied:
            return None
        if _origin(link_url) not in publisher_origins:
            return None
        try:
            _require_public_resolution(link_url, loopback_origin)
        except _RouteDenied:
            return None
        page = self._page_body(request, link_url, publisher_origins, loopback_origin)
        if page is None:
            return None
        title, published = _page_metadata(page.document)
        if title is None:
            return None
        return AcquiredArticle(
            request.source_id,
            request.publisher_id,
            request.title,
            None,
            title,
            None,
            str(page.url),
            published,
            (),
            request.default_article_language,
            page.body,
            page.blocks,
            page.classification,
        )

    def _page_body(
        self,
        request: SourceRequest,
        link_url: httpx.URL,
        publisher_origins: set[_Origin],
        loopback_origin: _Origin | None,
    ) -> _PageBody | None:
        """Fetch and extract one publisher page under every acquisition safety rule.

        The single page route: robots, redirect discipline, SSRF pinning and the response
        size cap live in ``_get_permitted``; a declared canonical URL must itself pass the
        origin allowlist and public resolution before it may replace the link. Shared
        verbatim between the feed entry path and Near Miss recovery so the two routes can
        never drift apart.
        """

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
            return _PageBody(
                candidate, candidate_blocks, "verified_page_body", link_url, response.content
            )
        if candidate and request.allow_short_as_published:
            return _PageBody(
                candidate, candidate_blocks, "short_as_published", link_url, response.content
            )
        return None

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
