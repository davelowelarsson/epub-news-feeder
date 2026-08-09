from __future__ import annotations

from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import cast
from zipfile import ZIP_STORED, ZipFile

import pytest
from lxml import etree

from epub_news_feeder.delivery import deliver_local
from epub_news_feeder.epub import (
    ArticleInput,
    CorrectionInput,
    EditionInput,
    EditorialCitationInput,
    EditorialSentenceInput,
    EditorialSummaryInput,
    LinkInput,
    NavigationInput,
    PriorCoverageInput,
    SectionInput,
    SectionPointerInput,
    StoryArticleLinkInput,
    StoryHubInput,
    build_epub,
)
from epub_news_feeder.validation import validate_epub


def _edition() -> EditionInput:
    return EditionInput(
        title="Morning Briefing",
        identifier="morning-briefing-20260809",
        language="en",
        run_id="20260809T060000Z-ABCDEFGH",
        sections=(
            SectionInput(
                identifier="world",
                title="World",
                articles=(
                    ArticleInput(
                        identifier="article-1",
                        title="A complete report",
                        body="The complete publisher article body.",
                        source_name="Example News",
                        canonical_url="https://example.test/articles/1",
                        author="A. Reporter",
                    ),
                ),
            ),
        ),
    )


def test_ticket_11_build_epub_creates_a_readable_attributed_epub() -> None:
    epub_bytes = build_epub(_edition())

    with ZipFile(BytesIO(epub_bytes)) as archive:
        entries = archive.infolist()
        assert entries[0].filename == "mimetype"
        assert entries[0].compress_type == ZIP_STORED
        assert archive.read("mimetype") == b"application/epub+zip"
        assert {
            "META-INF/container.xml",
            "OEBPS/content.opf",
            "OEBPS/nav.xhtml",
            "OEBPS/styles.css",
        }.issubset(archive.namelist())
        package = etree.fromstring(archive.read("OEBPS/content.opf"))
        manifest_items = list(package.iter("{http://www.idpf.org/2007/opf}item"))
        spine_items = list(package.iter("{http://www.idpf.org/2007/opf}itemref"))
        assert any(
            item.get("href") == "nav.xhtml" and item.get("properties") == "nav"
            for item in manifest_items
        )
        assert len(spine_items) == 2
        nav = etree.fromstring(archive.read("OEBPS/nav.xhtml"))
        assert "Contents" in " ".join(text for text in nav.itertext() if isinstance(text, str))
        section_path = next(path for path in archive.namelist() if path.startswith("OEBPS/world-"))

        section = etree.fromstring(archive.read(section_path))
        rendered = " ".join(text for text in section.itertext() if isinstance(text, str))
        assert "The complete publisher article body." in rendered
        assert "A. Reporter" in rendered
        assert "Example News" in rendered
        assert "20260809T060000Z-ABCDEFGH" in rendered
        assert section.xpath("//*[local-name()='a']/@href") == ["https://example.test/articles/1"]


def test_verified_editorial_summary_is_labeled_and_cited() -> None:
    article = replace(
        _edition().sections[0].articles[0],
        language="sv",
        editorial_summary=EditorialSummaryInput(
            sentences=(
                EditorialSentenceInput(
                    text="Rapporten beskriver en verifierad utveckling.",
                    citations=(
                        EditorialCitationInput(
                            label="Example News",
                            canonical_url="https://example.test/articles/1",
                        ),
                    ),
                ),
            ),
        ),
    )
    edition = replace(
        _edition(),
        language="sv",
        sections=(replace(_edition().sections[0], articles=(article,)),),
    )

    with ZipFile(BytesIO(build_epub(edition))) as archive:
        section_path = next(path for path in archive.namelist() if path.startswith("OEBPS/world-"))
        section = etree.fromstring(archive.read(section_path))
        about_ai = etree.fromstring(archive.read("OEBPS/about-ai-summaries.xhtml"))
        package = etree.fromstring(archive.read("OEBPS/content.opf"))
        nav = etree.fromstring(archive.read("OEBPS/nav.xhtml"))
        all_xhtml = b" ".join(
            archive.read(path) for path in archive.namelist() if path.endswith(".xhtml")
        )

    rendered = " ".join(text for text in section.itertext() if isinstance(text, str))
    assert "AI-genererad sammanfattning" in rendered
    assert "Artikel från Example News" in rendered
    assert "Rättigheter: Upphovsrättsinformation saknas; se publicisten." in rendered
    assert "Rapporten beskriver en verifierad utveckling." in rendered
    assert "oberoende lokal verifierare" not in rendered
    article_node = cast(list[etree._Element], section.xpath("//*[local-name()='article']"))[0]
    summary_node = cast(
        list[etree._Element], section.xpath("//*[contains(@class, 'editorial-summary')]")
    )[0]
    publisher_node = cast(
        list[etree._Element], section.xpath("//*[contains(@class, 'publisher-content')]")
    )[0]
    assert article_node.get("lang") == "sv"
    assert summary_node.get("lang") == "sv"
    assert summary_node.get("role") == "note"
    assert publisher_node.get("lang") == "sv"
    assert section.xpath(
        "//*[contains(@class, 'editorial-summary')]//*[local-name()='a']/@href"
    ) == ["https://example.test/articles/1"]
    assert "Om AI-sammanfattningar" in " ".join(
        text for text in about_ai.itertext() if isinstance(text, str)
    )
    assert all_xhtml.count(b"granskas oberoende") == 1
    assert nav.xpath("//*[local-name()='a' and @href='about-ai-summaries.xhtml']")
    spine_ids = cast(
        list[str], package.xpath("//*[local-name()='spine']/*[local-name()='itemref']/@idref")
    )
    assert spine_ids[-1] == "about-ai-summaries"
    assert b"Excerpt From" not in all_xhtml
    assert b"This material may be protected by copyright" not in all_xhtml


def test_mixed_language_articles_localize_each_summary_independently() -> None:
    original = _edition().sections[0].articles[0]
    swedish = replace(
        original,
        identifier="article-sv",
        title="Svensk artikel",
        language="sv",
        editorial_summary=EditorialSummaryInput(
            (EditorialSentenceInput("En svensk sammanfattning.", ()),)
        ),
    )
    english = replace(
        original,
        identifier="article-en",
        title="English article",
        language="en",
        editorial_summary=EditorialSummaryInput(
            (EditorialSentenceInput("An English summary.", ()),)
        ),
    )
    edition = replace(
        _edition(),
        language="sv",
        sections=(replace(_edition().sections[0], articles=(swedish, english)),),
    )

    with ZipFile(BytesIO(build_epub(edition))) as archive:
        section_path = next(path for path in archive.namelist() if path.startswith("OEBPS/world-"))
        section = etree.fromstring(archive.read(section_path))

    summaries = cast(
        list[etree._Element], section.xpath("//*[contains(@class, 'editorial-summary')]")
    )
    assert [summary.get("lang") for summary in summaries] == ["sv", "en"]
    assert [
        " ".join(" ".join(text for text in summary.itertext() if isinstance(text, str)).split())
        for summary in summaries
    ] == [
        "AI-genererad sammanfattning En svensk sammanfattning.",
        "AI-genererad sammanfattning An English summary.",
    ]


def test_ticket_02_ticket_06_local_delivery_acknowledges_verified_copy(tmp_path: Path) -> None:
    epub_bytes = build_epub(_edition())

    receipt = deliver_local(epub_bytes, output_directory=tmp_path, filename="morning.epub")

    assert receipt.path == tmp_path / "morning.epub"
    assert receipt.path.read_bytes() == epub_bytes
    assert receipt.sha256 == "f402fe43cb0ec5fc7c9f0320b578b78dea00c144b0af4f44fcc6c672860a14f6"
    assert receipt.size_bytes == len(epub_bytes)
    assert list(tmp_path.iterdir()) == [receipt.path]


@pytest.mark.property
@pytest.mark.epubcheck
def test_ticket_09_ticket_11_epub_is_deterministic_with_notes_and_pointers() -> None:
    edition = replace(
        _edition(),
        notes=("One Source was temporarily unavailable.",),
        corrections=(
            CorrectionInput(
                "A complete report",
                "Example News",
                "https://example.test/articles/1",
                "correction",
                "2026-08-09",
            ),
        ),
        sections=(
            replace(
                _edition().sections[0],
                story_hubs=(
                    StoryHubInput(
                        "harbor-story",
                        (StoryArticleLinkInput("article-1", "A complete report", "Example News"),),
                        (
                            PriorCoverageInput(
                                "Earlier report",
                                "Other News",
                                "https://other.example/earlier",
                                "2026-08-01",
                            ),
                        ),
                    ),
                ),
            ),
            SectionInput(
                identifier="technology",
                title="Technology",
                pointers=(
                    SectionPointerInput(
                        article_identifier="article-1",
                        headline="A complete report",
                        source_name="Example News",
                    ),
                ),
            ),
        ),
    )

    first = build_epub(edition)
    second = build_epub(edition)

    assert first == second
    validate_epub(first)
    with ZipFile(BytesIO(first)) as archive:
        world_path = next(path for path in archive.namelist() if path.startswith("OEBPS/world-"))
        technology_path = next(
            path for path in archive.namelist() if path.startswith("OEBPS/technology-")
        )
        world = etree.fromstring(archive.read(world_path))
        technology = etree.fromstring(archive.read(technology_path))
        notes = etree.fromstring(archive.read("OEBPS/edition-notes.xhtml"))
        corrections = etree.fromstring(archive.read("OEBPS/corrections.xhtml"))
        article_ids = [
            identifier
            for element in world.iter("{http://www.w3.org/1999/xhtml}article")
            if (identifier := element.get("id")) is not None
        ]
        assert len(article_ids) == 1
        article_id = article_ids[0]

        technology_section_ids = cast(list[str], technology.xpath("//*[local-name()='h1']/@id"))
        technology_section_id = technology_section_ids[0]
        assert world.xpath("//*[local-name()='a']/@href") == [
            f"{Path(technology_path).name}#{technology_section_id}",
            "https://example.test/articles/1",
            f"#{article_id}",
            "https://other.example/earlier",
        ]
        world_text = " ".join(text for text in world.itertext() if isinstance(text, str))
        assert "Continuing coverage" in world_text
        assert "Earlier report" in world_text
        assert "old article body" not in world_text

        rendered = " ".join(text for text in technology.itertext() if isinstance(text, str))
        notes_text = " ".join(text for text in notes.itertext() if isinstance(text, str))
        assert "One Source was temporarily unavailable." in notes_text
        corrections_text = " ".join(
            text for text in corrections.itertext() if isinstance(text, str)
        )
        assert "Corrections and updates" in corrections_text
        assert "Read the publisher correction" in corrections_text
        assert "A complete report" in rendered
        assert "Example News: Also relevant to Technology" in rendered
        assert "Primary placement: World" in rendered
        assert technology.xpath("//*[local-name()='a']/@href") == [
            f"{Path(world_path).name}#{article_id}"
        ]


def test_local_delivery_never_overwrites_an_immutable_delivery_copy(tmp_path: Path) -> None:
    epub_bytes = build_epub(_edition())
    receipt = deliver_local(epub_bytes, output_directory=tmp_path, filename="morning.epub")

    with pytest.raises(FileExistsError):
        deliver_local(b"different bytes", output_directory=tmp_path, filename="morning.epub")

    assert receipt.path.read_bytes() == epub_bytes


@pytest.mark.acceptance
def test_ticket_11_navigation_preserves_nested_main_sections() -> None:
    edition = replace(
        _edition(),
        navigation=(
            NavigationInput(
                "news",
                "News",
                children=(NavigationInput("world", "World"),),
            ),
        ),
    )

    with ZipFile(BytesIO(build_epub(edition))) as archive:
        nav = etree.fromstring(archive.read("OEBPS/nav.xhtml"))

    labels = [text.strip() for text in nav.itertext() if isinstance(text, str) and text.strip()]
    assert labels == [
        "Morning Briefing",
        "Edition overview",
        "This edition contains 1 complete article and 0 metadata-only publisher links across 1 "
        "section.",
        "Contents",
        "News",
        "World",
        "A complete report — Example News",
    ]
    assert nav.xpath("//*[local-name()='span' and text()='News']")
    nested_lists = cast(
        list[etree._Element],
        nav.xpath("//*[local-name()='span' and text()='News']/../*[local-name()='ol']"),
    )
    assert len(nested_lists) == 1


def test_navigation_lists_each_canonical_rendition_beneath_its_leaf_section() -> None:
    edition = replace(
        _edition(),
        sections=(
            replace(
                _edition().sections[0],
                articles=(
                    _edition().sections[0].articles[0],
                    ArticleInput(
                        identifier="article-2",
                        title="A second complete report",
                        body="Another complete publisher article body.",
                        source_name="Other News",
                        canonical_url="https://other.example/articles/2",
                    ),
                ),
            ),
        ),
        navigation=(
            NavigationInput(
                "news",
                "News",
                children=(NavigationInput("world", "World"),),
            ),
        ),
    )

    with ZipFile(BytesIO(build_epub(edition))) as archive:
        nav = etree.fromstring(archive.read("OEBPS/nav.xhtml"))

    world_item = cast(
        list[etree._Element],
        nav.xpath("//*[local-name()='a' and text()='World']/.."),
    )[0]
    article_links = cast(
        list[etree._Element],
        world_item.xpath("./*[local-name()='ol']/*[local-name()='li']/*[local-name()='a']"),
    )
    assert [link.text for link in article_links] == [
        "A complete report — Example News",
        "A second complete report — Other News",
    ]
    assert all(".xhtml#article-" in cast(str, link.get("href")) for link in article_links)


def test_navigation_overviews_full_articles_and_metadata_only_publisher_links() -> None:
    edition = replace(
        _edition(),
        sections=(
            replace(
                _edition().sections[0],
                links=(
                    LinkInput(
                        identifier="radio-developing-story",
                        title="A developing local story",
                        source_name="Public Radio",
                        canonical_url="https://radio.example/news/developing-story",
                    ),
                ),
            ),
        ),
    )

    with ZipFile(BytesIO(build_epub(edition))) as archive:
        nav = etree.fromstring(archive.read("OEBPS/nav.xhtml"))

    rendered = " ".join(text for text in nav.itertext() if isinstance(text, str))
    assert (
        "This edition contains 1 complete article and 1 metadata-only publisher link "
        "across 1 section."
    ) in rendered
    publisher_links = cast(
        list[etree._Element],
        nav.xpath("//*[local-name()='a' and starts-with(text(), '[Publisher link]') ]"),
    )
    assert [link.text for link in publisher_links] == [
        "[Publisher link] A developing local story — Public Radio"
    ]
    assert ".xhtml#publisher-link-" in cast(str, publisher_links[0].get("href"))


def test_canonical_rendition_visibly_labels_publisher_metadata() -> None:
    article = replace(
        _edition().sections[0].articles[0],
        published_at="2026-08-08",
        copyright_notice="Copyright Example Media",
    )
    edition = replace(_edition(), sections=(replace(_edition().sections[0], articles=(article,)),))

    with ZipFile(BytesIO(build_epub(edition))) as archive:
        section_path = next(path for path in archive.namelist() if path.startswith("OEBPS/world-"))
        section = etree.fromstring(archive.read(section_path))

    rendered = " ".join(
        text.strip() for text in section.itertext() if isinstance(text, str) and text.strip()
    )
    assert "By A. Reporter" in rendered
    assert "Source: Example News" in rendered
    assert "Published by publisher: 2026-08-08" in rendered
    assert "Rights: Copyright Example Media" in rendered
    published = cast(
        list[etree._Element],
        section.xpath("//*[local-name()='time' and @datetime='2026-08-08']"),
    )
    assert [item.text for item in published] == ["2026-08-08"]
    publisher_route = cast(
        list[etree._Element],
        section.xpath("//*[local-name()='a' and text()='Read full article at publisher']"),
    )
    assert [link.get("href") for link in publisher_route] == ["https://example.test/articles/1"]


@pytest.mark.epubcheck
def test_metadata_only_item_shows_attribution_and_an_explicit_publisher_route() -> None:
    edition = replace(
        _edition(),
        sections=(
            replace(
                _edition().sections[0],
                links=(
                    LinkInput(
                        identifier="radio-developing-story",
                        title="A developing local story",
                        source_name="Public Radio",
                        canonical_url="https://radio.example/news/developing-story",
                        author="B. Broadcaster",
                        published_at="2026-08-09",
                    ),
                ),
            ),
        ),
    )

    epub_bytes = build_epub(edition)
    validate_epub(epub_bytes)
    with ZipFile(BytesIO(epub_bytes)) as archive:
        section_path = next(path for path in archive.namelist() if path.startswith("OEBPS/world-"))
        section = etree.fromstring(archive.read(section_path))

    publisher_item = cast(
        list[etree._Element],
        section.xpath("//*[local-name()='li' and starts-with(@id, 'publisher-link-')]"),
    )[0]
    rendered = " ".join(
        text.strip() for text in publisher_item.itertext() if isinstance(text, str) and text.strip()
    )
    assert "A developing local story" in rendered
    assert "Publisher link; this Edition does not reproduce the article text." in rendered
    assert "By B. Broadcaster" in rendered
    assert "Source: Public Radio" in rendered
    assert "Published by publisher: 2026-08-09" in rendered
    published = cast(
        list[etree._Element],
        publisher_item.xpath(".//*[local-name()='time' and @datetime='2026-08-09']"),
    )
    assert [item.text for item in published] == ["2026-08-09"]
    routes = cast(
        list[etree._Element],
        publisher_item.xpath(".//*[local-name()='a' and text()='Read report at publisher']"),
    )
    assert [link.get("href") for link in routes] == ["https://radio.example/news/developing-story"]
    assert not publisher_item.xpath(".//*[local-name()='article']")


def test_swedish_metadata_only_item_localizes_reader_facing_labels() -> None:
    link = LinkInput(
        identifier="ekot-report",
        title="En svensk rapport",
        source_name="Sveriges Radio Ekot",
        canonical_url="https://www.sverigesradio.se/artikel/example",
        language="sv",
        author="Ekot",
        published_at="2026-08-09",
    )
    edition = replace(
        _edition(),
        language="sv",
        sections=(replace(_edition().sections[0], articles=(), links=(link,)),),
    )

    with ZipFile(BytesIO(build_epub(edition))) as archive:
        section_path = next(path for path in archive.namelist() if path.startswith("OEBPS/world-"))
        rendered = archive.read(section_path).decode()

    assert "Länk till publicisten; den här utgåvan återger inte artikeltexten." in rendered
    assert "Av Ekot" in rendered
    assert "Källa: Sveriges Radio Ekot" in rendered
    assert "Publicerad av publicisten:" in rendered
    assert "Läs rapporten hos Sveriges Radio Ekot" in rendered


def test_missing_publisher_metadata_is_explicit_in_articles_and_link_briefs() -> None:
    article = replace(
        _edition().sections[0].articles[0],
        author=None,
        published_at=None,
        copyright_notice=None,
    )
    edition = replace(
        _edition(),
        sections=(
            replace(
                _edition().sections[0],
                articles=(article,),
                links=(
                    LinkInput(
                        identifier="publisher-item",
                        title="Publisher report",
                        source_name="Public Radio",
                        canonical_url="https://radio.example/news/report",
                    ),
                ),
            ),
        ),
    )

    with ZipFile(BytesIO(build_epub(edition))) as archive:
        section_path = next(path for path in archive.namelist() if path.startswith("OEBPS/world-"))
        section = etree.fromstring(archive.read(section_path))

    rendered = " ".join(
        text.strip() for text in section.itertext() if isinstance(text, str) and text.strip()
    )
    assert rendered.count("Byline: Not supplied by publisher") == 2
    assert rendered.count("Published by publisher: Date not supplied") == 2
    assert "Rights: Copyright information not supplied; see publisher." in rendered


def test_publisher_link_navigation_target_is_stable_when_links_are_reordered() -> None:
    first = LinkInput(
        identifier="first-report",
        title="First report",
        source_name="Public Radio",
        canonical_url="https://radio.example/news/first",
    )
    second = LinkInput(
        identifier="second-report",
        title="Second report",
        source_name="Public Radio",
        canonical_url="https://radio.example/news/second",
    )

    def navigation_targets(links: tuple[LinkInput, ...]) -> dict[str | None, str | None]:
        edition = replace(
            _edition(),
            sections=(replace(_edition().sections[0], links=links),),
        )
        with ZipFile(BytesIO(build_epub(edition))) as archive:
            nav = etree.fromstring(archive.read("OEBPS/nav.xhtml"))
        anchors = cast(
            list[etree._Element],
            nav.xpath("//*[local-name()='a' and starts-with(text(), '[Publisher link]') ]"),
        )
        return {anchor.text: anchor.get("href") for anchor in anchors}

    assert navigation_targets((first, second)) == navigation_targets((second, first))


def _bilingual_edition(publication_language: str) -> EditionInput:
    """One Edition whose Article Language deliberately differs from its Publication Language."""

    article = ArticleInput(
        identifier="article-1",
        title="En svensk artikel",
        body="Publicistens artikeltext.",
        source_name="Sveriges Television",
        canonical_url="https://www.svt.test/artikel/1",
        language="sv",
        author="S. Reporter",
        published_at="2026-08-08",
        copyright_notice="Copyright SVT",
        materially_updated=True,
        editorial_summary=EditorialSummaryInput(
            (
                EditorialSentenceInput(
                    text="Sammanfattningen orienterar läsaren.",
                    citations=(
                        EditorialCitationInput(
                            "En svensk artikel", "https://www.svt.test/artikel/1"
                        ),
                    ),
                ),
            )
        ),
    )
    return EditionInput(
        title="Morgonbriefing",
        identifier="morning-briefing-20260809",
        language=publication_language,
        run_id="20260809T060000Z-ABCDEFGH",
        notes=("En källa var tillfälligt otillgänglig.",),
        corrections=(
            CorrectionInput(
                "En svensk artikel",
                "Sveriges Television",
                "https://www.svt.test/artikel/1",
                "correction",
                "2026-08-09",
            ),
        ),
        sections=(
            SectionInput(
                identifier="world",
                title="Världen",
                articles=(article,),
                has_edition_note=True,
                story_hubs=(
                    StoryHubInput(
                        "harbor-story",
                        (
                            StoryArticleLinkInput(
                                "article-1", "En svensk artikel", "Sveriges Television"
                            ),
                        ),
                        (
                            PriorCoverageInput(
                                "Tidigare rapport",
                                "Sveriges Television",
                                "https://www.svt.test/artikel/0",
                                "2026-08-01",
                            ),
                        ),
                    ),
                ),
            ),
            SectionInput(
                identifier="technology",
                title="Teknik",
                pointers=(
                    SectionPointerInput(
                        article_identifier="article-1",
                        headline="En svensk artikel",
                        source_name="Sveriges Television",
                    ),
                ),
            ),
        ),
    )


def _edition_text(epub_bytes: bytes) -> str:
    """All reader-visible text across every document in the Edition, whitespace-normalized."""

    rendered: list[str] = []
    with ZipFile(BytesIO(epub_bytes)) as archive:
        for name in archive.namelist():
            if not name.endswith(".xhtml"):
                continue
            document = etree.fromstring(archive.read(name))
            rendered.extend(
                text for text in document.itertext() if isinstance(text, str) and text.strip()
            )
    return " ".join(" ".join(rendered).split())


def test_generator_labels_follow_publication_language_not_article_language() -> None:
    epub_bytes = build_epub(_bilingual_edition("en"))

    rendered = _edition_text(epub_bytes)

    # Generator chrome speaks to the reader in the Publication Language.
    for english_label in (
        "Edition overview",
        "Contents",
        "Edition notes",
        "Corrections and updates",
        "Read the publisher correction",
        "Some reporting was unavailable; read the Edition notes",
        "Updated since your previous Edition",
        "By S. Reporter",
        "Source: Sveriges Television",
        "Published by publisher:",
        "Rights: Copyright SVT",
        "Article from Sveriges Television",
        "Read full article at publisher",
        "Also in this Edition",
        "Primary placement: Världen",
        "Continuing coverage",
        "In this Edition",
        "Prior coverage",
        "AI-generated summary",
        "About AI summaries",
    ):
        assert english_label in rendered, english_label

    # No Swedish chrome anywhere, even though every Article is Swedish.
    for swedish_label in (
        "Källa:",
        "Av S. Reporter",
        "Publicerad av publicisten:",
        "Rättigheter:",
        "Artikel från",
        "Läs hela artikeln hos",
        "Uppdaterad sedan din förra utgåva",
        "Innehåll",
        "AI-genererad sammanfattning",
    ):
        assert swedish_label not in rendered, swedish_label

    # Publisher text keeps its own language markers.
    with ZipFile(BytesIO(epub_bytes)) as archive:
        section_path = next(path for path in archive.namelist() if path.startswith("OEBPS/world-"))
        section = etree.fromstring(archive.read(section_path))
    assert cast(list[str], section.xpath("//*[contains(@class,'publisher-content')]/@lang")) == [
        "sv"
    ]
    assert cast(list[str], section.xpath("//*[contains(@class,'editorial-summary')]/@lang")) == [
        "sv"
    ]
    assert "Publicistens artikeltext." in _edition_text(epub_bytes)
    assert "Sammanfattningen orienterar läsaren." in _edition_text(epub_bytes)


def test_generator_labels_follow_publication_language_in_swedish() -> None:
    english_article = replace(
        _bilingual_edition("sv").sections[0].articles[0],
        title="An English article",
        body="The publisher article body.",
        language="en",
        editorial_summary=EditorialSummaryInput(
            (EditorialSentenceInput("An English summary.", ()),)
        ),
    )
    edition = replace(
        _bilingual_edition("sv"),
        sections=(
            replace(_bilingual_edition("sv").sections[0], articles=(english_article,)),
            _bilingual_edition("sv").sections[1],
        ),
    )

    rendered = _edition_text(build_epub(edition))

    for swedish_label in (
        "Innehåll",
        "Utgåvans noteringar",
        "Rättelser och uppdateringar",
        "Läs publicistens rättelse",
        "Uppdaterad sedan din förra utgåva",
        "Av S. Reporter",
        "Källa: Sveriges Television",
        "Publicerad av publicisten:",
        "Rättigheter: Copyright SVT",
        "Artikel från Sveriges Television",
        "Läs hela artikeln hos Sveriges Television",
        "Även i den här utgåvan",
        "Primär placering: Världen",
        "Fortsatt bevakning",
        "AI-genererad sammanfattning",
        "Om AI-sammanfattningar",
    ):
        assert swedish_label in rendered, swedish_label

    for english_label in (
        "Contents",
        "Source: Sveriges Television",
        "By S. Reporter",
        "Read full article at publisher",
        "Updated since your previous Edition",
        "AI-generated summary",
    ):
        assert english_label not in rendered, english_label

    # The English Article's own text and language markers survive the Swedish chrome.
    assert "The publisher article body." in rendered
    with ZipFile(BytesIO(build_epub(edition))) as archive:
        section_path = next(path for path in archive.namelist() if path.startswith("OEBPS/world-"))
        section = etree.fromstring(archive.read(section_path))
    assert cast(list[str], section.xpath("//*[contains(@class,'publisher-content')]/@lang")) == [
        "en"
    ]


def test_update_notice_is_a_generator_label_in_both_publication_languages() -> None:
    unchanged = replace(_bilingual_edition("en").sections[0].articles[0], materially_updated=False)

    english = _edition_text(build_epub(_bilingual_edition("en")))
    swedish = _edition_text(build_epub(_bilingual_edition("sv")))
    without = _edition_text(
        build_epub(
            replace(
                _bilingual_edition("en"),
                sections=(
                    replace(_bilingual_edition("en").sections[0], articles=(unchanged,)),
                    _bilingual_edition("en").sections[1],
                ),
            )
        )
    )

    assert "Updated since your previous Edition" in english
    assert "Uppdaterad sedan din förra utgåva" in swedish
    assert "Updated since your previous Edition" not in without
    assert "Uppdaterad sedan din förra utgåva" not in without


def test_unlisted_publication_language_falls_back_to_english_chrome() -> None:
    """Deliberate: translations must exist before a third Publication Language is configured."""

    rendered = _edition_text(build_epub(_bilingual_edition("de")))

    assert "Contents" in rendered
    assert "Source: Sveriges Television" in rendered
    assert "Innehåll" not in rendered
