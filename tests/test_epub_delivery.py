from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import cast
from zipfile import ZIP_STORED, ZipFile

import pytest
from lxml import etree

from epub_news_feeder.application import edition_filename
from epub_news_feeder.delivery import deliver_local
from epub_news_feeder.epub import (
    ArticleInput,
    BodyBlock,
    BriefInput,
    CorrectionInput,
    EditionInput,
    EditorialCitationInput,
    EditorialSentenceInput,
    EditorialSummaryInput,
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
    assert receipt.sha256 == "ddc5111e1d9d882edbe5028f1bf7149b1d4f56715f968585cd9467d177d960f4"
    assert receipt.size_bytes == len(epub_bytes)
    assert list(tmp_path.iterdir()) == [receipt.path]


def test_edition_filename_leads_with_the_date_and_names_the_publication() -> None:
    """A Kobo truncates a long filename from the right, so the date has to come first."""

    filename = edition_filename(
        publication_id="daily",
        generated_at=datetime(2026, 8, 9, 6, 0, tzinfo=UTC),
        run_id="20260809T060000Z-AAAAAAAA",
    )

    assert filename == "2026-08-09-daily-AAAAAAAA.epub"


def test_edition_filename_slugifies_a_publication_identifier() -> None:
    filename = edition_filename(
        publication_id="Local Reality Check!",
        generated_at=datetime(2026, 8, 9, 6, 0, tzinfo=UTC),
        run_id="20260809T060000Z-AAAAAAAA",
    )

    assert filename == "2026-08-09-local-reality-check-AAAAAAAA.epub"


@pytest.mark.parametrize(
    "publication_id",
    ["a-publication-identifier-far-longer-than-any-reader-can-see", "—", ""],
)
def test_edition_filename_stays_short_and_never_degenerates(publication_id: str) -> None:
    """Whatever the identifier, the name stays readable and keeps its three parts."""

    filename = edition_filename(
        publication_id=publication_id,
        generated_at=datetime(2026, 8, 9, 6, 0, tzinfo=UTC),
        run_id="20260809T060000Z-AAAAAAAA",
    )

    assert filename.startswith("2026-08-09-")
    assert filename.endswith("-AAAAAAAA.epub")
    assert len(filename) <= 48
    assert "--" not in filename


def test_edition_filename_sorts_chronologically_and_stays_unique_within_a_day() -> None:
    """Two Editions of one Publication on one day must not collide, and must sort by date."""

    morning = edition_filename(
        publication_id="daily",
        generated_at=datetime(2026, 8, 9, 6, 0, tzinfo=UTC),
        run_id="20260809T060000Z-AAAAAAAA",
    )
    evening = edition_filename(
        publication_id="daily",
        generated_at=datetime(2026, 8, 9, 18, 0, tzinfo=UTC),
        run_id="20260809T180000Z-BBBBBBBB",
    )
    tomorrow = edition_filename(
        publication_id="daily",
        generated_at=datetime(2026, 8, 10, 6, 0, tzinfo=UTC),
        run_id="20260810T060000Z-CCCCCCCC",
    )

    assert morning != evening
    assert sorted([tomorrow, evening, morning])[-1] == tomorrow


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
        "This edition contains 1 complete article across 1 section.",
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


def _briefs() -> tuple[BriefInput, ...]:
    return (
        BriefInput(
            identifier="radio-developing-story",
            title="A developing local story",
            source_name="Public Radio",
            canonical_url="https://radio.example/news/developing-story",
            published_at="2026-08-09",
            language="en",
        ),
        BriefInput(
            identifier="radio-earlier-story",
            title="An earlier local story",
            source_name="Public Radio",
            canonical_url="https://radio.example/news/earlier-story",
            published_at="2026-08-08",
            language="en",
        ),
    )


def _in_brief_chapter(epub_bytes: bytes) -> etree._Element:
    with ZipFile(BytesIO(epub_bytes)) as archive:
        return etree.fromstring(archive.read("OEBPS/in-brief.xhtml"))


@pytest.mark.epubcheck
def test_briefs_gather_into_one_chapter_with_the_link_on_the_source_name() -> None:
    edition = replace(_edition(), briefs=_briefs())

    epub_bytes = build_epub(edition)
    validate_epub(epub_bytes)
    chapter = _in_brief_chapter(epub_bytes)

    rendered = " ".join(
        text.strip() for text in chapter.itertext() if isinstance(text, str) and text.strip()
    )
    assert "In Brief" in rendered
    assert "A developing local story" in rendered

    # The headline is plain text; the publisher route sits on the source name.
    headlines = cast(list[etree._Element], chapter.xpath("//*[@class='brief-headline']"))
    assert [headline.text for headline in headlines] == [
        "A developing local story",
        "An earlier local story",
    ]
    assert not any(headline.xpath(".//*[local-name()='a']") for headline in headlines)
    routes = cast(
        list[etree._Element], chapter.xpath("//*[@class='brief-meta']/*[local-name()='a']")
    )
    assert [route.text for route in routes] == ["Public Radio", "Public Radio"]
    assert [route.get("href") for route in routes] == [
        "https://radio.example/news/developing-story",
        "https://radio.example/news/earlier-story",
    ]

    # None of the deleted chrome survives anywhere in the Edition.
    with ZipFile(BytesIO(epub_bytes)) as archive:
        all_xhtml = b" ".join(
            archive.read(path) for path in archive.namelist() if path.endswith(".xhtml")
        )
    for removed in (
        b"[Publisher link]",
        b"Publisher link; this Edition does not reproduce the article text.",
        b"Read report at publisher",
        b"More reporting at publishers",
    ):
        assert removed not in all_xhtml, removed


def test_in_brief_has_a_single_navigation_entry_and_sits_after_corrections() -> None:
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
        briefs=_briefs(),
    )

    with ZipFile(BytesIO(build_epub(edition))) as archive:
        nav = etree.fromstring(archive.read("OEBPS/nav.xhtml"))
        package = etree.fromstring(archive.read("OEBPS/content.opf"))

    entries = cast(list[str], nav.xpath("//*[local-name()='a']/@href"))
    assert entries.count("in-brief.xhtml") == 1
    assert entries.index("corrections.xhtml") < entries.index("in-brief.xhtml")

    spine = cast(
        list[str], package.xpath("//*[local-name()='spine']/*[local-name()='itemref']/@idref")
    )
    assert spine.index("corrections") < spine.index("in-brief")
    assert spine.index("in-brief") < spine.index(
        next(item for item in spine if item.startswith("section-"))
    )
    assert any(
        item.get("href") == "in-brief.xhtml"
        for item in package.iter("{http://www.idpf.org/2007/opf}item")
    )


def test_an_edition_without_briefs_omits_the_chapter_entirely() -> None:
    with ZipFile(BytesIO(build_epub(_edition()))) as archive:
        names = archive.namelist()
        nav = etree.fromstring(archive.read("OEBPS/nav.xhtml"))
        package = etree.fromstring(archive.read("OEBPS/content.opf"))

    assert "OEBPS/in-brief.xhtml" not in names
    assert "in-brief.xhtml" not in cast(list[str], nav.xpath("//*[local-name()='a']/@href"))
    assert "in-brief" not in cast(
        list[str], package.xpath("//*[local-name()='spine']/*[local-name()='itemref']/@idref")
    )


def test_briefs_are_absent_from_section_documents_and_section_navigation() -> None:
    edition = replace(_edition(), briefs=_briefs())

    epub_bytes = build_epub(edition)
    with ZipFile(BytesIO(epub_bytes)) as archive:
        section_path = next(path for path in archive.namelist() if path.startswith("OEBPS/world-"))
        section = archive.read(section_path).decode()

    assert "A developing local story" not in section
    assert "Public Radio" not in section


def test_in_brief_chapter_follows_the_publication_language() -> None:
    edition = replace(_edition(), language="sv", briefs=_briefs())

    rendered = " ".join(
        text.strip()
        for text in _in_brief_chapter(build_epub(edition)).itertext()
        if isinstance(text, str) and text.strip()
    )

    assert "I korthet" in rendered
    assert "Följ ett källnamn" in rendered
    assert "In Brief" not in rendered
    # The headline keeps its own language marker even though the chrome is Swedish.
    headline = cast(
        list[etree._Element],
        _in_brief_chapter(build_epub(edition)).xpath("//*[@class='brief-headline']"),
    )[0]
    assert headline.get("lang") == "en"


def test_edition_overview_counts_briefs_apart_from_articles() -> None:
    with ZipFile(BytesIO(build_epub(replace(_edition(), briefs=_briefs())))) as archive:
        nav = etree.fromstring(archive.read("OEBPS/nav.xhtml"))

    rendered = " ".join(" ".join(text for text in nav.itertext() if isinstance(text, str)).split())
    assert "This edition contains 1 complete article across 1 section." in rendered
    assert "It also carries 2 briefs in the In Brief chapter." in rendered


def test_brief_identifiers_must_be_unique() -> None:
    duplicated = (_briefs()[0], replace(_briefs()[1], identifier="radio-developing-story"))

    with pytest.raises(ValueError, match="Brief identifiers"):
        build_epub(replace(_edition(), briefs=duplicated))


def _cover(epub_bytes: bytes) -> etree._Element:
    with ZipFile(BytesIO(epub_bytes)) as archive:
        return etree.fromstring(archive.read("OEBPS/cover.svg"))


@pytest.mark.epubcheck
def test_every_edition_carries_a_typographic_cover() -> None:
    edition = replace(_edition(), briefs=_briefs(), edition_date="2026-08-09")

    epub_bytes = build_epub(edition)
    validate_epub(epub_bytes)

    with ZipFile(BytesIO(epub_bytes)) as archive:
        package = etree.fromstring(archive.read("OEBPS/content.opf"))
    manifest = [
        item
        for item in package.iter("{http://www.idpf.org/2007/opf}item")
        if item.get("href") == "cover.svg"
    ]
    assert len(manifest) == 1
    assert manifest[0].get("properties") == "cover-image"
    assert manifest[0].get("media-type") == "image/svg+xml"

    # The cover is an image item, never a reading document.
    spine = cast(
        list[str], package.xpath("//*[local-name()='spine']/*[local-name()='itemref']/@idref")
    )
    assert manifest[0].get("id") not in spine


def test_cover_carries_title_date_and_both_counts_in_the_publication_language() -> None:
    edition = replace(_edition(), briefs=_briefs(), edition_date="2026-08-09")

    rendered = " ".join(
        " ".join(
            text for text in _cover(build_epub(edition)).itertext() if isinstance(text, str)
        ).split()
    )

    assert "Morning Briefing" in rendered
    assert "2026-08-09" in rendered
    assert "1 complete article" in rendered
    assert "2 briefs" in rendered


def test_cover_counts_are_derived_from_the_edition_being_built() -> None:
    without_briefs = replace(_edition(), edition_date="2026-08-09")

    rendered = " ".join(
        " ".join(
            text for text in _cover(build_epub(without_briefs)).itertext() if isinstance(text, str)
        ).split()
    )

    assert "1 complete article" in rendered
    assert "brief" not in rendered


def test_cover_follows_the_publication_language() -> None:
    edition = replace(_edition(), language="sv", briefs=_briefs(), edition_date="2026-08-09")

    rendered = " ".join(
        " ".join(
            text for text in _cover(build_epub(edition)).itertext() if isinstance(text, str)
        ).split()
    )

    assert "komplett artikel" in rendered
    assert "notiser" in rendered
    assert "complete article" not in rendered


def test_cover_is_accessible_and_carries_no_imagery_or_remote_reference() -> None:
    edition = replace(_edition(), briefs=_briefs(), edition_date="2026-08-09")

    epub_bytes = build_epub(edition)
    cover = _cover(epub_bytes)

    assert cover.get("role") == "img"
    label = cover.get("aria-label")
    assert label is not None and "Morning Briefing" in label and "2026-08-09" in label
    titles = cast(list[etree._Element], cover.xpath("//*[local-name()='title']"))
    descriptions = cast(list[etree._Element], cover.xpath("//*[local-name()='desc']"))
    assert titles and descriptions

    with ZipFile(BytesIO(epub_bytes)) as archive:
        raw = archive.read("OEBPS/cover.svg")
    # No imagery, no publisher media, no embedded font, no fetchable reference of any kind.
    # The SVG namespace URI is an identifier, never retrieved, so it does not count.
    for forbidden in (b"<image", b"@font-face", b"base64", b"url("):
        assert forbidden not in raw, forbidden
    assert not cover.xpath("//@href | //@src | //@*[local-name()='href']")


def test_cover_is_deterministic_and_greyscale_safe() -> None:
    edition = replace(_edition(), briefs=_briefs(), edition_date="2026-08-09")

    first = build_epub(edition)
    second = build_epub(edition)

    assert first == second
    with ZipFile(BytesIO(first)) as archive:
        raw = archive.read("OEBPS/cover.svg").decode()
    # Only black, white and greys: colour never carries meaning on e-ink.
    colours = set(re.findall(r'(?:fill|stroke)="([^"]+)"', raw))
    assert colours <= {"none", "#000000", "#ffffff", "#f4f4f4", "#767676"}, colours


def test_publisher_body_blocks_render_with_kind_specific_semantics() -> None:
    article = replace(
        _edition().sections[0].articles[0],
        body=(
            "Intro paragraph.\n\nA pull quote.\n\nFirst item.\n\nSecond item.\n\ngit status --short"
        ),
        blocks=(
            BodyBlock("paragraph", "Intro paragraph."),
            BodyBlock("quote", "A pull quote."),
            BodyBlock("list", "First item."),
            BodyBlock("list", "Second item."),
            BodyBlock("code", "git status --short"),
            BodyBlock("diagram", "flowchart TD task-->result"),
        ),
    )
    edition = replace(_edition(), sections=(replace(_edition().sections[0], articles=(article,)),))

    with ZipFile(BytesIO(build_epub(edition))) as archive:
        section_path = next(path for path in archive.namelist() if path.startswith("OEBPS/world-"))
        section = etree.fromstring(archive.read(section_path))

    publisher = cast(
        list[etree._Element], section.xpath("//*[contains(@class, 'publisher-content')]")
    )[0]
    rendered = " ".join(text for text in publisher.itertext() if isinstance(text, str))
    assert "flowchart" not in rendered
    quote_paragraphs = cast(
        list[etree._Element],
        publisher.xpath(".//*[local-name()='blockquote']/*[local-name()='p']"),
    )
    assert [paragraph.text for paragraph in quote_paragraphs] == ["A pull quote."]
    list_elements = cast(list[etree._Element], publisher.xpath(".//*[local-name()='ul']"))
    assert len(list_elements) == 1
    list_items = cast(list[etree._Element], list_elements[0].xpath("./*[local-name()='li']"))
    assert [item.text for item in list_items] == ["First item.", "Second item."]
    code_blocks = cast(list[etree._Element], publisher.xpath(".//*[local-name()='pre']"))
    assert [block.text for block in code_blocks] == ["git status --short"]
    assert code_blocks[0].get("class") == "publisher-code"
    assert publisher.xpath(".//*[local-name()='a']/@href") == ["https://example.test/articles/1"]


def test_isolated_list_block_renders_as_a_single_item_list() -> None:
    article = replace(
        _edition().sections[0].articles[0],
        blocks=(
            BodyBlock("paragraph", "Before."),
            BodyBlock("list", "Only item."),
            BodyBlock("paragraph", "After."),
        ),
    )
    edition = replace(_edition(), sections=(replace(_edition().sections[0], articles=(article,)),))

    with ZipFile(BytesIO(build_epub(edition))) as archive:
        section_path = next(path for path in archive.namelist() if path.startswith("OEBPS/world-"))
        section = etree.fromstring(archive.read(section_path))

    lists = cast(list[etree._Element], section.xpath("//*[local-name()='ul']"))
    assert len(lists) == 1
    items = cast(list[etree._Element], lists[0].xpath("./*[local-name()='li']"))
    assert [item.text for item in items] == ["Only item."]


def test_unrecognised_body_block_kind_is_omitted_never_rendered_as_text() -> None:
    article = replace(
        _edition().sections[0].articles[0],
        blocks=(
            BodyBlock("paragraph", "Visible paragraph."),
            BodyBlock("table", "Should never appear."),
        ),
    )
    edition = replace(_edition(), sections=(replace(_edition().sections[0], articles=(article,)),))

    with ZipFile(BytesIO(build_epub(edition))) as archive:
        section_path = next(path for path in archive.namelist() if path.startswith("OEBPS/world-"))
        rendered = archive.read(section_path).decode()

    assert "Visible paragraph." in rendered
    assert "Should never appear." not in rendered


@pytest.mark.epubcheck
def test_diagram_block_is_omitted_but_canonical_route_remains_and_epub_validates() -> None:
    article = replace(
        _edition().sections[0].articles[0],
        blocks=(
            BodyBlock("paragraph", "Reported context."),
            BodyBlock("diagram", "flowchart TD a-->b"),
        ),
    )
    edition = replace(_edition(), sections=(replace(_edition().sections[0], articles=(article,)),))

    epub_bytes = build_epub(edition)
    validate_epub(epub_bytes)
    with ZipFile(BytesIO(epub_bytes)) as archive:
        section_path = next(path for path in archive.namelist() if path.startswith("OEBPS/world-"))
        section = etree.fromstring(archive.read(section_path))

    rendered = " ".join(text for text in section.itertext() if isinstance(text, str))
    assert "flowchart" not in rendered
    assert section.xpath(
        "//*[local-name()='a' and text()='Read full article at publisher']/@href"
    ) == ["https://example.test/articles/1"]


def _about_ai(epub_bytes: bytes) -> str:
    with ZipFile(BytesIO(epub_bytes)) as archive:
        document = etree.fromstring(archive.read("OEBPS/about-ai-summaries.xhtml"))
    return " ".join(" ".join(text for text in document.itertext() if isinstance(text, str)).split())


def _summarised_article() -> ArticleInput:
    return replace(
        _edition().sections[0].articles[0],
        editorial_summary=EditorialSummaryInput(
            (EditorialSentenceInput("A generated orientation.", ()),)
        ),
    )


def test_end_matter_names_the_sources_excluded_from_summaries() -> None:
    edition = replace(
        _edition(),
        sections=(replace(_edition().sections[0], articles=(_summarised_article(),)),),
        editorial_excluded_sources=("Ars Technica",),
    )

    rendered = _about_ai(build_epub(edition))

    assert "Ars Technica" in rendered
    # Factual, not a complaint about the publisher.
    assert "refuse" not in rendered.casefold()
    assert "unfortunately" not in rendered.casefold()


def test_no_excluded_source_means_no_exclusion_line() -> None:
    edition = replace(
        _edition(),
        sections=(replace(_edition().sections[0], articles=(_summarised_article(),)),),
    )

    rendered = _about_ai(build_epub(edition))

    assert "not generated" not in rendered.casefold()
    assert "Ars Technica" not in rendered


def test_end_matter_appears_for_an_excluded_source_even_with_no_summary() -> None:
    edition = replace(_edition(), editorial_excluded_sources=("Ars Technica",))

    epub_bytes = build_epub(edition)

    with ZipFile(BytesIO(epub_bytes)) as archive:
        assert "OEBPS/about-ai-summaries.xhtml" in archive.namelist()
    assert "Ars Technica" in _about_ai(epub_bytes)


def test_no_summaries_and_no_exclusions_omits_the_end_matter() -> None:
    with ZipFile(BytesIO(build_epub(_edition()))) as archive:
        assert "OEBPS/about-ai-summaries.xhtml" not in archive.namelist()


def test_excluded_sources_carry_no_per_article_marker() -> None:
    edition = replace(
        _edition(),
        sections=(replace(_edition().sections[0], articles=(_summarised_article(),)),),
        editorial_excluded_sources=("Ars Technica",),
    )

    with ZipFile(BytesIO(build_epub(edition))) as archive:
        section_path = next(path for path in archive.namelist() if path.startswith("OEBPS/world-"))
        section = archive.read(section_path).decode()

    assert "Ars Technica" not in section


def test_exclusion_line_follows_the_publication_language() -> None:
    edition = replace(
        _edition(),
        language="sv",
        sections=(replace(_edition().sections[0], articles=(_summarised_article(),)),),
        editorial_excluded_sources=("Ars Technica",),
    )

    rendered = _about_ai(build_epub(edition))

    assert "Sammanfattningar" in rendered
    assert "Ars Technica" in rendered
