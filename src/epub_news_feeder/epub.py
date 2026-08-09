"""Deterministic EPUB 3.3 construction from reader-facing Edition inputs."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from lxml import etree

_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_EPUB_NS = "http://www.idpf.org/2007/ops"
_OPF_NS = "http://www.idpf.org/2007/opf"
_XHTML_NS = "http://www.w3.org/1999/xhtml"
_XML_NS = "http://www.w3.org/XML/1998/namespace"
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True)
class EditorialCitationInput:
    """One publisher Article cited by generated editorial prose."""

    label: str
    canonical_url: str


@dataclass(frozen=True)
class EditorialSentenceInput:
    text: str
    citations: tuple[EditorialCitationInput, ...]


@dataclass(frozen=True)
class EditorialSummaryInput:
    sentences: tuple[EditorialSentenceInput, ...]


@dataclass(frozen=True)
class ArticleInput:
    """One complete, attributed Canonical Rendition."""

    identifier: str
    title: str
    body: str
    source_name: str
    canonical_url: str
    language: str | None = None
    author: str | None = None
    published_at: str | None = None
    materially_updated: bool = False
    copyright_notice: str | None = None
    editorial_summary: EditorialSummaryInput | None = None


@dataclass(frozen=True)
class SectionPointerInput:
    """A compact secondary placement that links to a Canonical Rendition."""

    article_identifier: str
    headline: str
    source_name: str


@dataclass(frozen=True)
class BriefInput:
    """One Publisher Link Brief: a headline and a route back to its publisher.

    A Brief is a rights outcome, never a failure outcome. It carries no body because its
    Source permits a publisher route and not reproduction.
    """

    identifier: str
    title: str
    source_name: str
    canonical_url: str
    published_at: str | None = None
    language: str | None = None


@dataclass(frozen=True)
class StoryArticleLinkInput:
    article_identifier: str
    headline: str
    source_name: str


@dataclass(frozen=True)
class PriorCoverageInput:
    headline: str
    source_name: str
    canonical_url: str
    published_at: str


@dataclass(frozen=True)
class StoryHubInput:
    identifier: str
    current_articles: tuple[StoryArticleLinkInput, ...]
    prior_coverage: tuple[PriorCoverageInput, ...] = ()


@dataclass(frozen=True)
class SectionInput:
    """An ordered reader-facing Section."""

    identifier: str
    title: str
    articles: tuple[ArticleInput, ...] = ()
    pointers: tuple[SectionPointerInput, ...] = ()
    has_edition_note: bool = False
    story_hubs: tuple[StoryHubInput, ...] = ()


@dataclass(frozen=True)
class NavigationInput:
    """One nested table-of-contents entry; only leaves map to Section documents."""

    identifier: str
    title: str
    children: tuple[NavigationInput, ...] = ()


@dataclass(frozen=True)
class CorrectionInput:
    """A body-free publisher Correction Notice that remains due until delivery."""

    title: str
    source_name: str
    canonical_url: str
    kind: str
    signaled_at: str


@dataclass(frozen=True)
class EditionInput:
    """All deterministic inputs required to construct one EPUB Edition."""

    title: str
    identifier: str
    language: str
    run_id: str
    sections: tuple[SectionInput, ...]
    navigation: tuple[NavigationInput, ...] = ()
    notes: tuple[str, ...] = ()
    corrections: tuple[CorrectionInput, ...] = ()
    briefs: tuple[BriefInput, ...] = ()
    modified_at: str = "1980-01-01T00:00:00Z"


def build_epub(edition: EditionInput) -> bytes:
    """Return a byte-stable, non-DRM EPUB 3.3 archive for *edition*."""

    _validate(edition)
    section_paths = {
        section.identifier: _section_path(section.identifier) for section in edition.sections
    }
    article_locations = {
        article.identifier: (section.identifier, _article_fragment(article.identifier))
        for section in edition.sections
        for article in section.articles
    }
    related_sections: dict[str, tuple[str, ...]] = {}
    for article_id in article_locations:
        related_sections[article_id] = tuple(
            section.identifier
            for section in edition.sections
            if any(pointer.article_identifier == article_id for pointer in section.pointers)
        )
    section_titles = {section.identifier: section.title for section in edition.sections}

    members: list[tuple[str, bytes, int]] = [
        ("mimetype", b"application/epub+zip", ZIP_STORED),
        ("META-INF/container.xml", _container_document(), ZIP_DEFLATED),
        ("OEBPS/content.opf", _package_document(edition, section_paths), ZIP_DEFLATED),
        ("OEBPS/nav.xhtml", _navigation_document(edition, section_paths), ZIP_DEFLATED),
        ("OEBPS/styles.css", _STYLESHEET, ZIP_DEFLATED),
    ]
    if edition.notes:
        members.append(("OEBPS/edition-notes.xhtml", _notes_document(edition), ZIP_DEFLATED))
    if edition.corrections:
        members.append(("OEBPS/corrections.xhtml", _corrections_document(edition), ZIP_DEFLATED))
    if edition.briefs:
        members.append(("OEBPS/in-brief.xhtml", _in_brief_document(edition), ZIP_DEFLATED))
    members.extend(
        (
            str(section_paths[section.identifier]),
            _section_document(
                edition,
                section,
                section_paths,
                article_locations,
                related_sections,
                section_titles,
            ),
            ZIP_DEFLATED,
        )
        for section in edition.sections
    )
    if _has_editorial_summaries(edition):
        members.append(
            (
                "OEBPS/about-ai-summaries.xhtml",
                _about_ai_document(edition),
                ZIP_DEFLATED,
            )
        )
    return _archive(members)


def _validate(edition: EditionInput) -> None:
    if not edition.sections:
        raise ValueError("An Edition must have at least one Section")
    section_ids = [section.identifier for section in edition.sections]
    if len(set(section_ids)) != len(section_ids) or any(not value for value in section_ids):
        raise ValueError("Section identifiers must be unique and non-empty")
    articles = [article.identifier for section in edition.sections for article in section.articles]
    if len(set(articles)) != len(articles) or any(not value for value in articles):
        raise ValueError("Article identifiers must be unique and non-empty")
    article_ids = set(articles)
    brief_ids = [brief.identifier for brief in edition.briefs]
    if len(set(brief_ids)) != len(brief_ids) or any(not value for value in brief_ids):
        raise ValueError("Brief identifiers must be unique and non-empty")
    for pointer in (pointer for section in edition.sections for pointer in section.pointers):
        if pointer.article_identifier not in article_ids:
            raise ValueError("Section Pointer target is not a Canonical Rendition")
    if edition.navigation:
        navigation_ids = tuple(_navigation_ids(edition.navigation))
        if len(navigation_ids) != len(set(navigation_ids)):
            raise ValueError("Navigation identifiers must be unique")
        leaf_ids = set(_navigation_leaf_ids(edition.navigation))
        if leaf_ids != set(section_ids):
            raise ValueError("Navigation leaves must match Edition Sections")


def _archive(members: list[tuple[str, bytes, int]]) -> bytes:
    output = BytesIO()
    with ZipFile(output, mode="w") as archive:
        for name, payload, compression in members:
            info = ZipInfo(name, date_time=_ZIP_TIMESTAMP)
            info.compress_type = compression
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payload)
    return output.getvalue()


def _container_document() -> bytes:
    root = etree.Element(
        f"{{{_CONTAINER_NS}}}container", nsmap={"container": _CONTAINER_NS}, version="1.0"
    )
    rootfiles = etree.SubElement(root, f"{{{_CONTAINER_NS}}}rootfiles")
    etree.SubElement(
        rootfiles,
        f"{{{_CONTAINER_NS}}}rootfile",
        attrib={
            "full-path": "OEBPS/content.opf",
            "media-type": "application/oebps-package+xml",
        },
    )
    return _serialize(root)


def _package_document(edition: EditionInput, section_paths: dict[str, PurePosixPath]) -> bytes:
    package = etree.Element(
        f"{{{_OPF_NS}}}package", nsmap={"opf": _OPF_NS, "dc": _DC_NS}, version="3.0"
    )
    package.set("unique-identifier", "edition-id")
    metadata = etree.SubElement(package, f"{{{_OPF_NS}}}metadata")
    identifier = etree.SubElement(metadata, f"{{{_DC_NS}}}identifier", id="edition-id")
    identifier.text = edition.identifier
    title = etree.SubElement(metadata, f"{{{_DC_NS}}}title")
    title.text = edition.title
    language = etree.SubElement(metadata, f"{{{_DC_NS}}}language")
    language.text = edition.language
    modified = etree.SubElement(metadata, f"{{{_OPF_NS}}}meta", property="dcterms:modified")
    modified.text = edition.modified_at

    manifest = etree.SubElement(package, f"{{{_OPF_NS}}}manifest")
    etree.SubElement(
        manifest,
        f"{{{_OPF_NS}}}item",
        id="nav",
        href="nav.xhtml",
        attrib={"media-type": "application/xhtml+xml", "properties": "nav"},
    )
    etree.SubElement(
        manifest,
        f"{{{_OPF_NS}}}item",
        id="styles",
        href="styles.css",
        attrib={"media-type": "text/css"},
    )
    if edition.notes:
        etree.SubElement(
            manifest,
            f"{{{_OPF_NS}}}item",
            id="edition-notes",
            href="edition-notes.xhtml",
            attrib={"media-type": "application/xhtml+xml"},
        )
    if edition.corrections:
        etree.SubElement(
            manifest,
            f"{{{_OPF_NS}}}item",
            id="corrections",
            href="corrections.xhtml",
            attrib={"media-type": "application/xhtml+xml"},
        )
    if edition.briefs:
        etree.SubElement(
            manifest,
            f"{{{_OPF_NS}}}item",
            id="in-brief",
            href="in-brief.xhtml",
            attrib={"media-type": "application/xhtml+xml"},
        )
    if _has_editorial_summaries(edition):
        etree.SubElement(
            manifest,
            f"{{{_OPF_NS}}}item",
            id="about-ai-summaries",
            href="about-ai-summaries.xhtml",
            attrib={"media-type": "application/xhtml+xml"},
        )
    for section in edition.sections:
        path = section_paths[section.identifier]
        etree.SubElement(
            manifest,
            f"{{{_OPF_NS}}}item",
            id=_manifest_id(section.identifier),
            href=str(path.relative_to("OEBPS")),
            attrib={"media-type": "application/xhtml+xml"},
        )
    spine = etree.SubElement(package, f"{{{_OPF_NS}}}spine")
    etree.SubElement(spine, f"{{{_OPF_NS}}}itemref", idref="nav")
    if edition.notes:
        etree.SubElement(spine, f"{{{_OPF_NS}}}itemref", idref="edition-notes")
    if edition.corrections:
        etree.SubElement(spine, f"{{{_OPF_NS}}}itemref", idref="corrections")
    if edition.briefs:
        etree.SubElement(spine, f"{{{_OPF_NS}}}itemref", idref="in-brief")
    for section in edition.sections:
        etree.SubElement(spine, f"{{{_OPF_NS}}}itemref", idref=_manifest_id(section.identifier))
    if _has_editorial_summaries(edition):
        etree.SubElement(spine, f"{{{_OPF_NS}}}itemref", idref="about-ai-summaries")
    return _serialize(package)


def _navigation_document(edition: EditionInput, section_paths: dict[str, PurePosixPath]) -> bytes:
    html, body = _xhtml_document(edition.title, edition.language)
    article_count = sum(len(section.articles) for section in edition.sections)
    overview = etree.SubElement(
        body,
        f"{{{_XHTML_NS}}}section",
        attrib={"class": "edition-overview", "aria-labelledby": "edition-overview-heading"},
    )
    overview_heading = etree.SubElement(
        overview, f"{{{_XHTML_NS}}}h1", id="edition-overview-heading"
    )
    overview_heading.text = _localized(edition.language, "overview_heading")
    overview_summary = etree.SubElement(overview, f"{{{_XHTML_NS}}}p")
    overview_summary.text = _localized(
        edition.language,
        "overview_summary",
        articles=_counted(edition.language, article_count, "article"),
        sections=_counted(edition.language, len(edition.sections), "section"),
    )
    if edition.briefs:
        overview_briefs = etree.SubElement(overview, f"{{{_XHTML_NS}}}p")
        overview_briefs.text = _localized(
            edition.language,
            "overview_briefs",
            briefs=_counted(edition.language, len(edition.briefs), "brief"),
        )
    nav = etree.SubElement(body, f"{{{_XHTML_NS}}}nav", attrib={f"{{{_EPUB_NS}}}type": "toc"})
    heading = etree.SubElement(nav, f"{{{_XHTML_NS}}}h1")
    heading.text = _localized(edition.language, "contents")
    ordered = etree.SubElement(nav, f"{{{_XHTML_NS}}}ol")
    if edition.notes:
        item = etree.SubElement(ordered, f"{{{_XHTML_NS}}}li")
        link = etree.SubElement(item, f"{{{_XHTML_NS}}}a", href="edition-notes.xhtml")
        link.text = _localized(edition.language, "edition_notes")
    if edition.corrections:
        item = etree.SubElement(ordered, f"{{{_XHTML_NS}}}li")
        link = etree.SubElement(item, f"{{{_XHTML_NS}}}a", href="corrections.xhtml")
        link.text = _localized(edition.language, "corrections")
    if edition.briefs:
        # One entry for the chapter, never one per headline: listing every Brief in the
        # contents is the same chrome the chapter exists to remove.
        item = etree.SubElement(ordered, f"{{{_XHTML_NS}}}li")
        link = etree.SubElement(item, f"{{{_XHTML_NS}}}a", href="in-brief.xhtml")
        link.text = _localized(edition.language, "in_brief")
    navigation = edition.navigation or tuple(
        NavigationInput(section.identifier, section.title) for section in edition.sections
    )
    sections = {section.identifier: section for section in edition.sections}
    _add_navigation_items(ordered, navigation, section_paths, sections, edition.language)
    if _has_editorial_summaries(edition):
        item = etree.SubElement(ordered, f"{{{_XHTML_NS}}}li")
        link = etree.SubElement(item, f"{{{_XHTML_NS}}}a", href="about-ai-summaries.xhtml")
        link.text = _localized(edition.language, "about_ai")
    return _serialize(html)


def _add_navigation_items(
    parent: etree._Element,
    entries: tuple[NavigationInput, ...],
    section_paths: dict[str, PurePosixPath],
    sections: dict[str, SectionInput],
    language: str,
) -> None:
    for entry in entries:
        item = etree.SubElement(parent, f"{{{_XHTML_NS}}}li")
        if entry.children:
            label = etree.SubElement(item, f"{{{_XHTML_NS}}}span")
            label.text = entry.title
            nested = etree.SubElement(item, f"{{{_XHTML_NS}}}ol")
            _add_navigation_items(nested, entry.children, section_paths, sections, language)
        else:
            link = etree.SubElement(
                item, f"{{{_XHTML_NS}}}a", href=str(section_paths[entry.identifier].name)
            )
            link.text = entry.title
            section = sections[entry.identifier]
            if section.articles:
                articles = etree.SubElement(item, f"{{{_XHTML_NS}}}ol")
                for article in section.articles:
                    article_item = etree.SubElement(articles, f"{{{_XHTML_NS}}}li")
                    article_link = etree.SubElement(
                        article_item,
                        f"{{{_XHTML_NS}}}a",
                        href=(
                            f"{section_paths[entry.identifier].name}"
                            f"#{_article_fragment(article.identifier)}"
                        ),
                    )
                    article_link.text = _localized(
                        language,
                        "article_nav",
                        title=article.title,
                        source_name=article.source_name,
                    )


def _navigation_ids(entries: tuple[NavigationInput, ...]) -> list[str]:
    return [entry.identifier for entry in entries for _ in (0,)] + [
        identifier for entry in entries for identifier in _navigation_ids(entry.children)
    ]


def _navigation_leaf_ids(entries: tuple[NavigationInput, ...]) -> list[str]:
    return [
        identifier
        for entry in entries
        for identifier in (
            _navigation_leaf_ids(entry.children) if entry.children else [entry.identifier]
        )
    ]


def _notes_document(edition: EditionInput) -> bytes:
    html, body = _xhtml_document(
        _localized(edition.language, "notes_title", edition_title=edition.title), edition.language
    )
    main = etree.SubElement(body, f"{{{_XHTML_NS}}}main")
    heading = etree.SubElement(main, f"{{{_XHTML_NS}}}h1")
    heading.text = _localized(edition.language, "edition_notes")
    for note in edition.notes:
        paragraph = etree.SubElement(main, f"{{{_XHTML_NS}}}p")
        paragraph.text = note
    return _serialize(html)


def _about_ai_document(edition: EditionInput) -> bytes:
    html, body = _xhtml_document(_localized(edition.language, "about_ai"), edition.language)
    body.set(f"{{{_EPUB_NS}}}type", "backmatter")
    main = etree.SubElement(body, f"{{{_XHTML_NS}}}main")
    section = etree.SubElement(
        main,
        f"{{{_XHTML_NS}}}section",
        attrib={"aria-labelledby": "about-ai-heading"},
    )
    heading = etree.SubElement(section, f"{{{_XHTML_NS}}}h1", id="about-ai-heading")
    heading.text = _localized(edition.language, "about_ai")
    paragraph = etree.SubElement(section, f"{{{_XHTML_NS}}}p")
    paragraph.text = _localized(edition.language, "ai_method")
    return _serialize(html)


def _in_brief_document(edition: EditionInput) -> bytes:
    """Render every Brief in one chapter: plain headline, one sub-line, nothing else.

    The publisher link sits on the source name rather than the headline, so a reader
    checking a headline has an obvious place to press and the headline stays unadorned.
    """

    html, body = _xhtml_document(
        _localized(edition.language, "in_brief_title", edition_title=edition.title),
        edition.language,
    )
    main = etree.SubElement(body, f"{{{_XHTML_NS}}}main")
    heading = etree.SubElement(main, f"{{{_XHTML_NS}}}h1")
    heading.text = _localized(edition.language, "in_brief")
    intro = etree.SubElement(main, f"{{{_XHTML_NS}}}p", attrib={"class": "in-brief-intro"})
    intro.text = _localized(edition.language, "in_brief_intro")
    roll = etree.SubElement(main, f"{{{_XHTML_NS}}}ul", attrib={"class": "in-brief"})
    for brief in edition.briefs:
        item_language = brief.language or "und"
        item = etree.SubElement(
            roll,
            f"{{{_XHTML_NS}}}li",
            id=_brief_fragment(brief.identifier),
        )
        headline = etree.SubElement(
            item,
            f"{{{_XHTML_NS}}}p",
            attrib={
                "class": "brief-headline",
                "lang": item_language,
                f"{{{_XML_NS}}}lang": item_language,
            },
        )
        headline.text = brief.title
        meta = etree.SubElement(item, f"{{{_XHTML_NS}}}p", attrib={"class": "brief-meta"})
        route = etree.SubElement(meta, f"{{{_XHTML_NS}}}a", href=brief.canonical_url)
        route.text = brief.source_name
        if brief.published_at:
            published = etree.SubElement(meta, f"{{{_XHTML_NS}}}time", datetime=brief.published_at)
            published.text = f" — {brief.published_at}"
    return _serialize(html)


def _corrections_document(edition: EditionInput) -> bytes:
    html, body = _xhtml_document(
        _localized(edition.language, "corrections_title", edition_title=edition.title),
        edition.language,
    )
    main = etree.SubElement(body, f"{{{_XHTML_NS}}}main")
    heading = etree.SubElement(main, f"{{{_XHTML_NS}}}h1")
    heading.text = _localized(edition.language, "corrections")
    for correction in edition.corrections:
        notice = etree.SubElement(main, f"{{{_XHTML_NS}}}article")
        title = etree.SubElement(notice, f"{{{_XHTML_NS}}}h2")
        title.text = correction.title
        detail = etree.SubElement(notice, f"{{{_XHTML_NS}}}p")
        detail.text = _localized(
            edition.language,
            "correction_detail",
            source_name=correction.source_name,
            kind=correction.kind,
            signaled_at=correction.signaled_at,
        )
        link = etree.SubElement(notice, f"{{{_XHTML_NS}}}a", href=correction.canonical_url)
        link.text = _localized(edition.language, "correction_link")
    return _serialize(html)


def _section_document(
    edition: EditionInput,
    section: SectionInput,
    section_paths: dict[str, PurePosixPath],
    article_locations: dict[str, tuple[str, str]],
    related_sections: dict[str, tuple[str, ...]],
    section_titles: dict[str, str],
) -> bytes:
    html, body = _xhtml_document(f"{edition.title} — {section.title}", edition.language)
    main = etree.SubElement(body, f"{{{_XHTML_NS}}}main")
    heading = etree.SubElement(main, f"{{{_XHTML_NS}}}h1", id=_section_fragment(section.identifier))
    heading.text = section.title
    if section.has_edition_note and edition.notes:
        notice = etree.SubElement(main, f"{{{_XHTML_NS}}}p", attrib={"class": "edition-note-link"})
        notice_link = etree.SubElement(notice, f"{{{_XHTML_NS}}}a", href="edition-notes.xhtml")
        notice_link.text = _localized(edition.language, "edition_note_link")
    for article in section.articles:
        _add_article(
            main,
            article,
            section.identifier,
            section_paths,
            related_sections,
            section_titles,
            edition.language,
        )
    for pointer in section.pointers:
        _add_pointer(
            main,
            pointer,
            section.identifier,
            section_paths,
            article_locations,
            section_titles,
            edition.language,
        )
    for hub in section.story_hubs:
        _add_story_hub(
            main, hub, section.identifier, section_paths, article_locations, edition.language
        )
    colophon = etree.SubElement(body, f"{{{_XHTML_NS}}}footer", attrib={"class": "colophon"})
    colophon.text = f"Run ID: {edition.run_id}"
    return _serialize(html)


def _xhtml_document(title: str, language: str) -> tuple[etree._Element, etree._Element]:
    html = etree.Element(
        f"{{{_XHTML_NS}}}html",
        nsmap={"xhtml": _XHTML_NS, "epub": _EPUB_NS},
        attrib={"lang": language, f"{{{_XML_NS}}}lang": language},
    )
    head = etree.SubElement(html, f"{{{_XHTML_NS}}}head")
    title_element = etree.SubElement(head, f"{{{_XHTML_NS}}}title")
    title_element.text = title
    etree.SubElement(
        head,
        f"{{{_XHTML_NS}}}link",
        rel="stylesheet",
        href="styles.css",
        type="text/css",
    )
    return html, etree.SubElement(html, f"{{{_XHTML_NS}}}body")


def _add_article(
    parent: etree._Element,
    article: ArticleInput,
    current_section: str,
    section_paths: dict[str, PurePosixPath],
    related_sections: dict[str, tuple[str, ...]],
    section_titles: dict[str, str],
    language: str,
) -> None:
    rendered = etree.SubElement(
        parent,
        f"{{{_XHTML_NS}}}article",
        id=_article_fragment(article.identifier),
    )
    article_language = article.language or "und"
    rendered.set("lang", article_language)
    rendered.set(f"{{{_XML_NS}}}lang", article_language)
    title = etree.SubElement(rendered, f"{{{_XHTML_NS}}}h2")
    title.text = article.title
    if article.materially_updated:
        update = etree.SubElement(rendered, f"{{{_XHTML_NS}}}p", attrib={"class": "update-label"})
        update.text = _localized(language, "update_notice")
    metadata = etree.SubElement(
        rendered, f"{{{_XHTML_NS}}}div", attrib={"class": "article-metadata"}
    )
    _add_publisher_metadata(
        metadata,
        author=article.author,
        source_name=article.source_name,
        published_at=article.published_at,
        language=language,
    )
    if article.copyright_notice:
        copyright_element = etree.SubElement(
            metadata, f"{{{_XHTML_NS}}}p", attrib={"class": "copyright"}
        )
        copyright_element.text = _localized(
            language,
            "rights_supplied",
            source_name=article.copyright_notice,
        )
    else:
        copyright_element = etree.SubElement(
            metadata, f"{{{_XHTML_NS}}}p", attrib={"class": "copyright"}
        )
        copyright_element.text = _localized(language, "rights_missing")
    if article.editorial_summary is not None:
        _add_editorial_summary(
            rendered,
            article.editorial_summary,
            article_language,
            article.identifier,
            language,
        )
    related = related_sections[article.identifier]
    if related:
        related_heading = etree.SubElement(rendered, f"{{{_XHTML_NS}}}h3")
        related_heading.text = _localized(language, "also_in_edition")
        related_list = etree.SubElement(rendered, f"{{{_XHTML_NS}}}ul")
        for section_id in related:
            item = etree.SubElement(related_list, f"{{{_XHTML_NS}}}li")
            href = (
                f"#{_section_fragment(section_id)}"
                if section_id == current_section
                else f"{section_paths[section_id].name}#{_section_fragment(section_id)}"
            )
            related_link = etree.SubElement(item, f"{{{_XHTML_NS}}}a", href=href)
            related_link.text = section_titles[section_id]
    publisher_heading_id = f"publisher-content-{_token(article.identifier)}"
    publisher = etree.SubElement(
        rendered,
        f"{{{_XHTML_NS}}}section",
        attrib={
            "class": "publisher-content",
            "aria-labelledby": publisher_heading_id,
            "lang": article_language,
            f"{{{_XML_NS}}}lang": article_language,
        },
    )
    publisher_heading = etree.SubElement(publisher, f"{{{_XHTML_NS}}}h3", id=publisher_heading_id)
    publisher_heading.set("lang", language)
    publisher_heading.set(f"{{{_XML_NS}}}lang", language)
    publisher_heading.text = _localized(
        language, "publisher_article", source_name=article.source_name
    )
    for paragraph_text in article.body.split("\n\n"):
        paragraph = etree.SubElement(publisher, f"{{{_XHTML_NS}}}p")
        paragraph.text = paragraph_text
    canonical = etree.SubElement(
        publisher,
        f"{{{_XHTML_NS}}}p",
        attrib={"class": "canonical-link", "lang": language, f"{{{_XML_NS}}}lang": language},
    )
    link = etree.SubElement(canonical, f"{{{_XHTML_NS}}}a", href=article.canonical_url)
    link.text = _localized(language, "read_at_publisher", source_name=article.source_name)


def _add_editorial_summary(
    parent: etree._Element,
    summary: EditorialSummaryInput,
    article_language: str,
    article_identifier: str,
    language: str,
) -> None:
    heading_id = f"summary-{_token(article_identifier)}"
    # The aside wraps generated prose, which follows the Article Language; its heading is a
    # generator label and carries the Publication Language of its own.
    aside = etree.SubElement(
        parent,
        f"{{{_XHTML_NS}}}aside",
        attrib={
            "class": "editorial-summary",
            "role": "note",
            "aria-labelledby": heading_id,
            "lang": article_language,
            f"{{{_XML_NS}}}lang": article_language,
            f"{{{_EPUB_NS}}}type": "annotation",
        },
    )
    heading = etree.SubElement(aside, f"{{{_XHTML_NS}}}h3", id=heading_id)
    heading.set("lang", language)
    heading.set(f"{{{_XML_NS}}}lang", language)
    heading.text = _localized(language, "ai_summary")
    for sentence in summary.sentences:
        paragraph = etree.SubElement(aside, f"{{{_XHTML_NS}}}p")
        paragraph.text = sentence.text
        for index, citation in enumerate(sentence.citations, start=1):
            citation_link = etree.SubElement(
                paragraph,
                f"{{{_XHTML_NS}}}a",
                href=citation.canonical_url,
                attrib={"class": "editorial-citation"},
            )
            citation_link.text = f" [{index}]"
            citation_link.set(
                "aria-label",
                _localized(language, "citation_label", index=index, label=citation.label),
            )


def _add_pointer(
    parent: etree._Element,
    pointer: SectionPointerInput,
    current_section: str,
    section_paths: dict[str, PurePosixPath],
    article_locations: dict[str, tuple[str, str]],
    section_titles: dict[str, str],
    language: str,
) -> None:
    target_section, target_fragment = article_locations[pointer.article_identifier]
    current_path = section_paths[current_section]
    target_path = section_paths[target_section]
    href = (
        f"{target_path.name}#{target_fragment}"
        if current_path != target_path
        else f"#{target_fragment}"
    )
    rendered = etree.SubElement(
        parent, f"{{{_XHTML_NS}}}aside", attrib={"class": "section-pointer"}
    )
    link = etree.SubElement(rendered, f"{{{_XHTML_NS}}}a", href=href)
    link.text = pointer.headline
    detail = etree.SubElement(rendered, f"{{{_XHTML_NS}}}p")
    detail.text = _localized(
        language,
        "pointer_detail",
        source_name=pointer.source_name,
        section_title=section_titles[current_section],
        primary_title=section_titles[target_section],
    )


def _add_publisher_metadata(
    parent: etree._Element,
    *,
    author: str | None,
    source_name: str,
    published_at: str | None,
    language: str,
) -> None:
    byline = etree.SubElement(parent, f"{{{_XHTML_NS}}}p", attrib={"class": "byline"})
    byline.text = (
        _localized(language, "byline", author=author)
        if author
        else _localized(language, "byline_missing")
    )
    source = etree.SubElement(parent, f"{{{_XHTML_NS}}}p", attrib={"class": "source"})
    source.text = _localized(language, "source", source_name=source_name)
    published = etree.SubElement(parent, f"{{{_XHTML_NS}}}p", attrib={"class": "published"})
    published.text = _localized(language, "published_prefix")
    if published_at:
        published_time = etree.SubElement(
            published,
            f"{{{_XHTML_NS}}}time",
            datetime=published_at,
        )
        published_time.text = published_at
    else:
        published.text += _localized(language, "published_missing")


def _add_story_hub(
    parent: etree._Element,
    hub: StoryHubInput,
    current_section: str,
    section_paths: dict[str, PurePosixPath],
    article_locations: dict[str, tuple[str, str]],
    language: str,
) -> None:
    rendered = etree.SubElement(
        parent,
        f"{{{_XHTML_NS}}}aside",
        id=f"story-{_token(hub.identifier)}",
        attrib={"class": "story-hub"},
    )
    heading = etree.SubElement(rendered, f"{{{_XHTML_NS}}}h2")
    heading.text = _localized(language, "continuing_coverage")
    current_heading = etree.SubElement(rendered, f"{{{_XHTML_NS}}}h3")
    current_heading.text = _localized(language, "in_this_edition")
    current_list = etree.SubElement(rendered, f"{{{_XHTML_NS}}}ul")
    for article in hub.current_articles:
        target_section, fragment = article_locations[article.article_identifier]
        href = (
            f"#{fragment}"
            if target_section == current_section
            else f"{section_paths[target_section].name}#{fragment}"
        )
        item = etree.SubElement(current_list, f"{{{_XHTML_NS}}}li")
        link = etree.SubElement(item, f"{{{_XHTML_NS}}}a", href=href)
        link.text = article.headline
        source = etree.SubElement(item, f"{{{_XHTML_NS}}}span")
        source.text = f" — {article.source_name}"
    if hub.prior_coverage:
        prior_heading = etree.SubElement(rendered, f"{{{_XHTML_NS}}}h3")
        prior_heading.text = _localized(language, "prior_coverage")
        prior_list = etree.SubElement(rendered, f"{{{_XHTML_NS}}}ul")
        for prior in hub.prior_coverage:
            item = etree.SubElement(prior_list, f"{{{_XHTML_NS}}}li")
            link = etree.SubElement(item, f"{{{_XHTML_NS}}}a", href=prior.canonical_url)
            link.text = prior.headline
            detail = etree.SubElement(item, f"{{{_XHTML_NS}}}span")
            detail.text = f" — {prior.source_name}, {prior.published_at}"


def _serialize(element: etree._Element) -> bytes:
    return etree.tostring(element, encoding="utf-8", xml_declaration=True, pretty_print=True)


def _has_editorial_summaries(edition: EditionInput) -> bool:
    return any(
        article.editorial_summary is not None
        for section in edition.sections
        for article in section.articles
    )


_ENGLISH_LABELS = {
    "about_ai": "About AI summaries",
    "ai_method": (
        "AI summaries are generated from cited publisher reporting and independently "
        "checked by a local verifier. Citation links identify the reporting used for each "
        "sentence."
    ),
    "ai_summary": "AI-generated summary",
    "also_in_edition": "Also in this Edition",
    "article_nav": "{title} — {source_name}",
    "byline": "By {author}",
    "byline_missing": "Byline: Not supplied by publisher",
    "citation_label": "Citation {index}: {label}",
    "contents": "Contents",
    "continuing_coverage": "Continuing coverage",
    "correction_detail": "{source_name} published a {kind} notice on {signaled_at}.",
    "correction_link": "Read the publisher correction",
    "corrections": "Corrections and updates",
    "corrections_title": "{edition_title} — Corrections and updates",
    "edition_note_link": "Some reporting was unavailable; read the Edition notes",
    "edition_notes": "Edition notes",
    "in_brief": "In Brief",
    "in_brief_intro": (
        "Headlines from publishers whose reporting this Edition does not reproduce. "
        "Follow a source name to read the report at its publisher."
    ),
    "in_brief_title": "{edition_title} — In Brief",
    "in_this_edition": "In this Edition",
    "notes_title": "{edition_title} — Edition notes",
    "overview_briefs": "It also carries {briefs} in the In Brief chapter.",
    "overview_heading": "Edition overview",
    "overview_summary": "This edition contains {articles} across {sections}.",
    "pointer_detail": (
        "{source_name}: Also relevant to {section_title}. Primary placement: {primary_title}"
    ),
    "prior_coverage": "Prior coverage",
    "published_missing": "Date not supplied",
    "published_prefix": "Published by publisher: ",
    "publisher_article": "Article from {source_name}",
    "read_at_publisher": "Read full article at publisher",
    "rights_missing": "Rights: Copyright information not supplied; see publisher.",
    "rights_supplied": "Rights: {source_name}",
    "source": "Source: {source_name}",
    "update_notice": "Updated since your previous Edition",
}

_SWEDISH_LABELS = {
    "about_ai": "Om AI-sammanfattningar",
    "ai_method": (
        "AI-sammanfattningar skapas från citerad publicistisk text och granskas "
        "oberoende av en lokal verifierare. Citatlänkarna visar vilket underlag som "
        "användes för varje mening."
    ),
    "ai_summary": "AI-genererad sammanfattning",
    "also_in_edition": "Även i den här utgåvan",
    "article_nav": "{title} — {source_name}",
    "byline": "Av {author}",
    "byline_missing": "Byline: Inte angiven av publicisten",
    "citation_label": "Källhänvisning {index}: {label}",
    "contents": "Innehåll",
    "continuing_coverage": "Fortsatt bevakning",
    "correction_detail": "{source_name} publicerade en notis av typen {kind} den {signaled_at}.",
    "correction_link": "Läs publicistens rättelse",
    "corrections": "Rättelser och uppdateringar",
    "corrections_title": "{edition_title} — rättelser och uppdateringar",
    "edition_note_link": "En del rapportering var inte tillgänglig; läs utgåvans noteringar",
    "edition_notes": "Utgåvans noteringar",
    "in_brief": "I korthet",
    "in_brief_intro": (
        "Rubriker från publicister vars rapportering den här utgåvan inte återger. "
        "Följ ett källnamn för att läsa rapporten hos publicisten."
    ),
    "in_brief_title": "{edition_title} — i korthet",
    "in_this_edition": "I den här utgåvan",
    "notes_title": "{edition_title} — utgåvans noteringar",
    "overview_briefs": "Den innehåller också {briefs} i kapitlet I korthet.",
    "overview_heading": "Utgåvans översikt",
    "overview_summary": "Den här utgåvan innehåller {articles} i {sections}.",
    "pointer_detail": (
        "{source_name}: Även relevant för {section_title}. Primär placering: {primary_title}"
    ),
    "prior_coverage": "Tidigare bevakning",
    "published_missing": "Datum saknas",
    "published_prefix": "Publicerad av publicisten: ",
    "publisher_article": "Artikel från {source_name}",
    "read_at_publisher": "Läs hela artikeln hos {source_name}",
    "rights_missing": "Rättigheter: Upphovsrättsinformation saknas; se publicisten.",
    "rights_supplied": "Rättigheter: {source_name}",
    "source": "Källa: {source_name}",
    "update_notice": "Uppdaterad sedan din förra utgåva",
}

_LABELS = {"en": _ENGLISH_LABELS, "sv": _SWEDISH_LABELS}

_COUNTED_NOUNS = {
    "en": {
        "article": ("complete article", "complete articles"),
        "brief": ("brief", "briefs"),
        "section": ("section", "sections"),
    },
    "sv": {
        "article": ("komplett artikel", "kompletta artiklar"),
        "brief": ("notis", "notiser"),
        "section": ("avsnitt", "avsnitt"),
    },
}


def _label_language(language: str) -> str:
    """Resolve a Publication Language to a label set.

    An unlisted language falls back to English silently and deliberately: translations must
    exist before a third Publication Language is configured. See issue #50.
    """

    return "sv" if language.casefold().split("-", 1)[0] == "sv" else "en"


def _localized(language: str, key: str, **params: object) -> str:
    """Render one generator label in the Publication Language.

    Every label the generator writes to the reader resolves here. Publisher text, Editorial
    Addition prose, and the `lang` attributes describing them keep the Article Language instead.
    """

    return _LABELS[_label_language(language)][key].format(**params)


def _counted(language: str, count: int, noun: str) -> str:
    singular, plural = _COUNTED_NOUNS[_label_language(language)][noun]
    return f"{count} {singular if count == 1 else plural}"


def _section_path(identifier: str) -> PurePosixPath:
    return PurePosixPath("OEBPS") / f"{_token(identifier)}.xhtml"


def _article_fragment(identifier: str) -> str:
    return f"article-{_token(identifier)}"


def _section_fragment(identifier: str) -> str:
    return f"section-{_token(identifier)}"


def _brief_fragment(identifier: str) -> str:
    return f"brief-{_token(identifier)}"


def _manifest_id(identifier: str) -> str:
    return f"section-{_token(identifier)}"


def _token(identifier: str) -> str:
    readable = "".join(
        character.lower() if character.isalnum() else "-" for character in identifier
    )
    readable = readable.strip("-") or "item"
    return f"{readable}-{sha256(identifier.encode()).hexdigest()[:12]}"


_STYLESHEET = (
    b"body { font-family: serif; line-height: 1.5; margin: 0 auto; max-width: 42em; "
    b"padding: 0 4%; }\n"
    b"h1 { font-size: 1.75em; line-height: 1.15; margin: 1.2em 0 0.6em; }\n"
    b"h2 { font-size: 1.35em; line-height: 1.2; margin: 1.5em 0 0.5em; }\n"
    b"h3 { font-size: 1.05em; line-height: 1.25; margin: 1.1em 0 0.35em; }\n"
    b"a { text-decoration-thickness: 0.08em; text-underline-offset: 0.12em; }\n"
    b"nav ol { padding-left: 1.35em; }\n"
    b"nav li { margin: 0.4em 0; }\n"
    b"nav ol ol { margin: 0.35em 0 0.8em; }\n"
    b"article { border-top: 0.08em solid; margin: 2.5em 0; padding-top: 0.4em; }\n"
    b".edition-overview { border-bottom: 0.12em solid; margin-bottom: 1.5em; "
    b"padding-bottom: 0.8em; }\n"
    b".article-metadata, .canonical-link, .colophon, .item-kind, .source { "
    b"font-family: sans-serif; font-size: 0.88em; }\n"
    b".article-metadata p { margin: 0.18em 0; }\n"
    b".in-brief { list-style: none; padding-left: 0; }\n"
    b".in-brief > li { border-top: 0.06em solid; margin: 1.1em 0; padding-top: 0.35em; }\n"
    b".brief-headline { font-size: 1.05em; line-height: 1.3; margin: 0 0 0.2em; }\n"
    b".brief-meta { font-family: sans-serif; font-size: 0.82em; margin: 0; }\n"
    b".in-brief-intro { font-family: sans-serif; font-size: 0.88em; }\n"
    b".section-pointer, .edition-notes { border-left: 0.2em solid; margin: 1em 0; "
    b"padding-left: 0.8em; }\n"
    b".editorial-summary { border: 0.12em solid; margin: 1.25em 0; "
    b"padding: 0.25em 0.85em 0.65em; }\n"
    b".publisher-content { border-top: 0.08em solid; border-bottom: 0.08em solid; "
    b"margin-top: 1.25em; padding: 0.35em 0 0.75em; }\n"
    b".colophon { border-top: 0.06em solid; margin-top: 3em; padding-top: 0.6em; }\n"
)
