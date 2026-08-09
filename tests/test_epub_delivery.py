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
        editorial_summary=EditorialSummaryInput(
            sentences=(
                EditorialSentenceInput(
                    text="The report describes a verified development.",
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
        sections=(replace(_edition().sections[0], articles=(article,)),),
    )

    with ZipFile(BytesIO(build_epub(edition))) as archive:
        section_path = next(path for path in archive.namelist() if path.startswith("OEBPS/world-"))
        section = etree.fromstring(archive.read(section_path))

    rendered = " ".join(text for text in section.itertext() if isinstance(text, str))
    assert "AI-generated summary" in rendered
    assert "independently checked by a local verifier" in rendered
    assert "The report describes a verified development." in rendered
    assert section.xpath(
        "//*[contains(@class, 'editorial-summary')]//*[local-name()='a']/@href"
    ) == ["https://example.test/articles/1"]


def test_ticket_02_ticket_06_local_delivery_acknowledges_verified_copy(tmp_path: Path) -> None:
    epub_bytes = build_epub(_edition())

    receipt = deliver_local(epub_bytes, output_directory=tmp_path, filename="morning.epub")

    assert receipt.path == tmp_path / "morning.epub"
    assert receipt.path.read_bytes() == epub_bytes
    assert receipt.sha256 == "86b04e825f221324fa0641e73c6e489f9e49f881d100a574919072658d5a5730"
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
                        relevance_reason="Relevant technology coverage",
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
            "https://example.test/articles/1",
            f"{Path(technology_path).name}#{technology_section_id}",
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
        assert "Example News: Relevant technology coverage" in rendered
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
