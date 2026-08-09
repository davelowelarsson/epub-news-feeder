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


def test_ticket_02_ticket_06_local_delivery_acknowledges_verified_copy(tmp_path: Path) -> None:
    epub_bytes = build_epub(_edition())

    receipt = deliver_local(epub_bytes, output_directory=tmp_path, filename="morning.epub")

    assert receipt.path == tmp_path / "morning.epub"
    assert receipt.path.read_bytes() == epub_bytes
    assert receipt.sha256 == "4d2ca7000311e628b484f12d3429c814a192cc8a9e5bfa9ba9b2f90292329c6a"
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
    assert labels == ["Morning Briefing", "Contents", "News", "World"]
    assert nav.xpath("//*[local-name()='span' and text()='News']")
    nested_lists = cast(
        list[etree._Element],
        nav.xpath("//*[local-name()='span' and text()='News']/../*[local-name()='ol']"),
    )
    assert len(nested_lists) == 1
