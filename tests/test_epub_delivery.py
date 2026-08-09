from __future__ import annotations

from dataclasses import replace
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

import pytest
from lxml import etree

from epub_news_feeder.delivery import deliver_local
from epub_news_feeder.epub import (
    ArticleInput,
    EditionInput,
    SectionInput,
    SectionPointerInput,
    build_epub,
)


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


def test_build_epub_creates_a_readable_attributed_epub() -> None:
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
        assert len(spine_items) == 1
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


def test_local_delivery_writes_and_acknowledges_the_verified_delivery_copy(tmp_path: Path) -> None:
    epub_bytes = build_epub(_edition())

    receipt = deliver_local(epub_bytes, output_directory=tmp_path, filename="morning.epub")

    assert receipt.path == tmp_path / "morning.epub"
    assert receipt.path.read_bytes() == epub_bytes
    assert receipt.sha256 == "3e029c210ffbbd498a1fe924d0840ef8ec0a3cf0f69049ffb60f32fda66fafed"
    assert receipt.size_bytes == len(epub_bytes)
    assert list(tmp_path.iterdir()) == [receipt.path]


def test_build_epub_is_deterministic_and_renders_notes_and_section_pointers() -> None:
    edition = replace(
        _edition(),
        notes=("One Source was temporarily unavailable.",),
        sections=(
            _edition().sections[0],
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
    with ZipFile(BytesIO(first)) as archive:
        world_path = next(path for path in archive.namelist() if path.startswith("OEBPS/world-"))
        technology_path = next(
            path for path in archive.namelist() if path.startswith("OEBPS/technology-")
        )
        world = etree.fromstring(archive.read(world_path))
        technology = etree.fromstring(archive.read(technology_path))
        article_ids = [
            identifier
            for element in world.iter("{http://www.w3.org/1999/xhtml}article")
            if (identifier := element.get("id")) is not None
        ]
        assert len(article_ids) == 1
        article_id = article_ids[0]

        rendered = " ".join(text for text in technology.itertext() if isinstance(text, str))
        assert "One Source was temporarily unavailable." in rendered
        assert "A complete report" in rendered
        assert "Example News: Relevant technology coverage" in rendered
        assert technology.xpath("//*[local-name()='a']/@href") == [
            f"{Path(world_path).name}#{article_id}"
        ]


def test_local_delivery_never_overwrites_an_immutable_delivery_copy(tmp_path: Path) -> None:
    epub_bytes = build_epub(_edition())
    receipt = deliver_local(epub_bytes, output_directory=tmp_path, filename="morning.epub")

    with pytest.raises(FileExistsError):
        deliver_local(b"different bytes", output_directory=tmp_path, filename="morning.epub")

    assert receipt.path.read_bytes() == epub_bytes
