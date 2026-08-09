from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from epub_news_feeder.selection import (
    Candidate,
    Policy,
    PublicationRequest,
    SectionCandidate,
    SectionRequest,
    place_articles,
    select_publication,
)


def candidate(
    article_id: str,
    source_id: str,
    *,
    hours_old: int,
    interest: int = 0,
    essential: bool = False,
    muted: bool = False,
    relevance: int = 0,
) -> SectionCandidate:
    article = Candidate(
        article_id=article_id,
        source_id=source_id,
        title=f"Article {article_id}",
        canonical_url=f"https://{source_id}.example/{article_id}",
        published_at=datetime(2026, 8, 9, 6, tzinfo=UTC) - timedelta(hours=hours_old),
        source_weight=5,
    )
    return SectionCandidate(
        article=article,
        relevance=relevance,
        interest_score=interest,
        essential=essential,
        muted=muted,
    )


@pytest.mark.acceptance
def test_ticket_07_coverage_selection_respects_plurality_and_publication_minimum() -> None:
    technology = SectionRequest(
        section_id="technology",
        title="Technology",
        order=0,
        policy=Policy.COVERAGE,
        max_articles=4,
        candidates=(
            candidate("a-essential", "source-a", hours_old=5, essential=True),
            candidate("a-new", "source-a", hours_old=0),
            candidate("a-old", "source-a", hours_old=2),
            candidate("b-new", "source-b", hours_old=1),
            candidate("b-old", "source-b", hours_old=3),
        ),
    )

    result = select_publication(
        PublicationRequest(max_articles=4, min_articles=3, sections=(technology,))
    )

    assert result.meets_minimum
    assert not result.partial
    selected = result.for_section("technology")
    assert selected[0].article.article_id == "a-essential"
    assert len(selected) == 4
    assert {slot.article.source_id for slot in selected} == {"source-a", "source-b"}
    assert sum(slot.article.source_id == "source-a" for slot in selected) == 2


@pytest.mark.acceptance
def test_ticket_07_below_minimum_cannot_publish() -> None:
    sparse = SectionRequest(
        section_id="sparse",
        title="Sparse",
        order=0,
        policy=Policy.COVERAGE,
        max_articles=6,
        candidates=(candidate("only", "source", hours_old=0),),
    )

    result = select_publication(
        PublicationRequest(max_articles=6, min_articles=2, sections=(sparse,))
    )

    assert not result.meets_minimum
    assert result.partial
    assert result.unique_article_ids == ("only",)


@pytest.mark.acceptance
def test_ticket_08_interest_changes_rank_mute_excludes_and_discovery_survives() -> None:
    interests = SectionRequest(
        section_id="interests",
        title="Interests",
        order=0,
        policy=Policy.INTEREST,
        max_articles=5,
        discovery_percent=0.2,
        candidates=(
            candidate("preferred-1", "a", hours_old=4, interest=10),
            candidate("preferred-2", "a", hours_old=3, interest=9),
            candidate("preferred-3", "b", hours_old=2, interest=8),
            candidate("discovery", "b", hours_old=0, interest=0),
            candidate("negative", "c", hours_old=1, interest=-5),
            candidate("muted", "c", hours_old=0, interest=100, muted=True),
        ),
    )

    result = select_publication(
        PublicationRequest(max_articles=5, min_articles=1, sections=(interests,))
    )

    ids = [slot.article.article_id for slot in result.for_section("interests")]
    assert "muted" not in ids
    assert "discovery" in ids
    assert ids.index("preferred-1") < ids.index("negative")


@pytest.mark.acceptance
def test_ticket_09_one_canonical_placement_has_reciprocal_section_pointers() -> None:
    shared_primary = candidate("shared", "source", hours_old=0, relevance=9)
    shared_secondary = candidate("shared", "source", hours_old=0, relevance=4)
    result = select_publication(
        PublicationRequest(
            max_articles=3,
            min_articles=1,
            sections=(
                SectionRequest(
                    "technology",
                    "Technology",
                    0,
                    Policy.COVERAGE,
                    2,
                    candidates=(shared_primary, candidate("tech", "source", hours_old=1)),
                ),
                SectionRequest(
                    "world",
                    "World",
                    1,
                    Policy.COVERAGE,
                    2,
                    candidates=(shared_secondary, candidate("world", "other", hours_old=1)),
                ),
            ),
        )
    )

    placements = place_articles(result)
    shared = placements["shared"]
    assert shared.primary_section_id == "technology"
    assert shared.pointer_section_ids == ("world",)
    assert shared.relevant_section_ids == ("technology", "world")
