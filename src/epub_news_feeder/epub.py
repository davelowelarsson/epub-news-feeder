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
class ArticleInput:
    """One complete, attributed Canonical Rendition."""

    identifier: str
    title: str
    body: str
    source_name: str
    canonical_url: str
    author: str | None = None
    published_at: str | None = None
    update_label: str | None = None
    copyright_notice: str | None = None


@dataclass(frozen=True)
class SectionPointerInput:
    """A compact secondary placement that links to a Canonical Rendition."""

    article_identifier: str
    headline: str
    source_name: str
    relevance_reason: str


@dataclass(frozen=True)
class LinkInput:
    """Attributed metadata-only Source item that opens at the publisher."""

    title: str
    source_name: str
    canonical_url: str


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
    links: tuple[LinkInput, ...] = ()
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
    for section in edition.sections:
        etree.SubElement(spine, f"{{{_OPF_NS}}}itemref", idref=_manifest_id(section.identifier))
    return _serialize(package)


def _navigation_document(edition: EditionInput, section_paths: dict[str, PurePosixPath]) -> bytes:
    html, body = _xhtml_document(edition.title, edition.language)
    nav = etree.SubElement(body, f"{{{_XHTML_NS}}}nav", attrib={f"{{{_EPUB_NS}}}type": "toc"})
    heading = etree.SubElement(nav, f"{{{_XHTML_NS}}}h1")
    heading.text = "Contents"
    ordered = etree.SubElement(nav, f"{{{_XHTML_NS}}}ol")
    if edition.notes:
        item = etree.SubElement(ordered, f"{{{_XHTML_NS}}}li")
        link = etree.SubElement(item, f"{{{_XHTML_NS}}}a", href="edition-notes.xhtml")
        link.text = "Edition notes"
    if edition.corrections:
        item = etree.SubElement(ordered, f"{{{_XHTML_NS}}}li")
        link = etree.SubElement(item, f"{{{_XHTML_NS}}}a", href="corrections.xhtml")
        link.text = "Corrections and updates"
    navigation = edition.navigation or tuple(
        NavigationInput(section.identifier, section.title) for section in edition.sections
    )
    _add_navigation_items(ordered, navigation, section_paths)
    return _serialize(html)


def _add_navigation_items(
    parent: etree._Element,
    entries: tuple[NavigationInput, ...],
    section_paths: dict[str, PurePosixPath],
) -> None:
    for entry in entries:
        item = etree.SubElement(parent, f"{{{_XHTML_NS}}}li")
        if entry.children:
            label = etree.SubElement(item, f"{{{_XHTML_NS}}}span")
            label.text = entry.title
            nested = etree.SubElement(item, f"{{{_XHTML_NS}}}ol")
            _add_navigation_items(nested, entry.children, section_paths)
        else:
            link = etree.SubElement(
                item, f"{{{_XHTML_NS}}}a", href=str(section_paths[entry.identifier].name)
            )
            link.text = entry.title


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
    html, body = _xhtml_document(f"{edition.title} — Edition notes", edition.language)
    main = etree.SubElement(body, f"{{{_XHTML_NS}}}main")
    heading = etree.SubElement(main, f"{{{_XHTML_NS}}}h1")
    heading.text = "Edition notes"
    for note in edition.notes:
        paragraph = etree.SubElement(main, f"{{{_XHTML_NS}}}p")
        paragraph.text = note
    return _serialize(html)


def _corrections_document(edition: EditionInput) -> bytes:
    html, body = _xhtml_document(f"{edition.title} — Corrections and updates", edition.language)
    main = etree.SubElement(body, f"{{{_XHTML_NS}}}main")
    heading = etree.SubElement(main, f"{{{_XHTML_NS}}}h1")
    heading.text = "Corrections and updates"
    for correction in edition.corrections:
        notice = etree.SubElement(main, f"{{{_XHTML_NS}}}article")
        title = etree.SubElement(notice, f"{{{_XHTML_NS}}}h2")
        title.text = correction.title
        detail = etree.SubElement(notice, f"{{{_XHTML_NS}}}p")
        detail.text = (
            f"{correction.source_name} published a {correction.kind} notice "
            f"on {correction.signaled_at}."
        )
        link = etree.SubElement(notice, f"{{{_XHTML_NS}}}a", href=correction.canonical_url)
        link.text = "Read the publisher correction"
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
        notice_link.text = "Some reporting was unavailable; read the Edition notes"
    for article in section.articles:
        _add_article(
            main, article, section.identifier, section_paths, related_sections, section_titles
        )
    for pointer in section.pointers:
        _add_pointer(
            main,
            pointer,
            section.identifier,
            section_paths,
            article_locations,
            section_titles,
        )
    for hub in section.story_hubs:
        _add_story_hub(main, hub, section.identifier, section_paths, article_locations)
    if section.links:
        links_heading = etree.SubElement(main, f"{{{_XHTML_NS}}}h2")
        links_heading.text = "Links from metadata-only Sources"
        links = etree.SubElement(main, f"{{{_XHTML_NS}}}ul", attrib={"class": "source-links"})
        for link in section.links:
            _add_source_link(links, link)
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
) -> None:
    rendered = etree.SubElement(
        parent,
        f"{{{_XHTML_NS}}}article",
        id=_article_fragment(article.identifier),
    )
    title = etree.SubElement(rendered, f"{{{_XHTML_NS}}}h2")
    title.text = article.title
    if article.update_label:
        update = etree.SubElement(rendered, f"{{{_XHTML_NS}}}p", attrib={"class": "update-label"})
        update.text = article.update_label
    attribution = etree.SubElement(rendered, f"{{{_XHTML_NS}}}p", attrib={"class": "attribution"})
    attribution.text = f"{article.author} · " if article.author else ""
    source = etree.SubElement(attribution, f"{{{_XHTML_NS}}}span")
    source.text = article.source_name
    if article.published_at:
        published = etree.SubElement(attribution, f"{{{_XHTML_NS}}}span")
        published.text = f" · Published {article.published_at}"
    if article.copyright_notice:
        copyright_element = etree.SubElement(
            attribution, f"{{{_XHTML_NS}}}span", attrib={"class": "copyright"}
        )
        copyright_element.text = f" · {article.copyright_notice}"
    canonical = etree.SubElement(rendered, f"{{{_XHTML_NS}}}p", attrib={"class": "canonical-link"})
    link = etree.SubElement(canonical, f"{{{_XHTML_NS}}}a", href=article.canonical_url)
    link.text = "Read at publisher"
    related = related_sections[article.identifier]
    if related:
        related_heading = etree.SubElement(rendered, f"{{{_XHTML_NS}}}h3")
        related_heading.text = "Also in this Edition"
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
    for paragraph_text in article.body.split("\n\n"):
        paragraph = etree.SubElement(rendered, f"{{{_XHTML_NS}}}p")
        paragraph.text = paragraph_text


def _add_pointer(
    parent: etree._Element,
    pointer: SectionPointerInput,
    current_section: str,
    section_paths: dict[str, PurePosixPath],
    article_locations: dict[str, tuple[str, str]],
    section_titles: dict[str, str],
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
    detail.text = (
        f"{pointer.source_name}: {pointer.relevance_reason}. "
        f"Primary placement: {section_titles[target_section]}"
    )


def _add_source_link(parent: etree._Element, source_link: LinkInput) -> None:
    item = etree.SubElement(parent, f"{{{_XHTML_NS}}}li")
    link = etree.SubElement(item, f"{{{_XHTML_NS}}}a", href=source_link.canonical_url)
    link.text = source_link.title
    source = etree.SubElement(item, f"{{{_XHTML_NS}}}span")
    source.text = f" — {source_link.source_name}"


def _add_story_hub(
    parent: etree._Element,
    hub: StoryHubInput,
    current_section: str,
    section_paths: dict[str, PurePosixPath],
    article_locations: dict[str, tuple[str, str]],
) -> None:
    rendered = etree.SubElement(
        parent,
        f"{{{_XHTML_NS}}}aside",
        id=f"story-{_token(hub.identifier)}",
        attrib={"class": "story-hub"},
    )
    heading = etree.SubElement(rendered, f"{{{_XHTML_NS}}}h2")
    heading.text = "Continuing coverage"
    current_heading = etree.SubElement(rendered, f"{{{_XHTML_NS}}}h3")
    current_heading.text = "In this Edition"
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
        prior_heading.text = "Prior coverage"
        prior_list = etree.SubElement(rendered, f"{{{_XHTML_NS}}}ul")
        for prior in hub.prior_coverage:
            item = etree.SubElement(prior_list, f"{{{_XHTML_NS}}}li")
            link = etree.SubElement(item, f"{{{_XHTML_NS}}}a", href=prior.canonical_url)
            link.text = prior.headline
            detail = etree.SubElement(item, f"{{{_XHTML_NS}}}span")
            detail.text = f" — {prior.source_name}, {prior.published_at}"


def _serialize(element: etree._Element) -> bytes:
    return etree.tostring(element, encoding="utf-8", xml_declaration=True, pretty_print=False)


def _section_path(identifier: str) -> PurePosixPath:
    return PurePosixPath("OEBPS") / f"{_token(identifier)}.xhtml"


def _article_fragment(identifier: str) -> str:
    return f"article-{_token(identifier)}"


def _section_fragment(identifier: str) -> str:
    return f"section-{_token(identifier)}"


def _manifest_id(identifier: str) -> str:
    return f"section-{_token(identifier)}"


def _token(identifier: str) -> str:
    readable = "".join(
        character.lower() if character.isalnum() else "-" for character in identifier
    )
    readable = readable.strip("-") or "item"
    return f"{readable}-{sha256(identifier.encode()).hexdigest()[:12]}"


_STYLESHEET = (
    b"body { font-family: serif; line-height: 1.45; }\n"
    b"article { margin: 2em 0; }\n"
    b".attribution, .canonical-link, .colophon { font-size: 0.9em; }\n"
    b".section-pointer, .edition-notes { border-left: 0.2em solid; margin: 1em 0; "
    b"padding-left: 0.8em; }\n"
)
