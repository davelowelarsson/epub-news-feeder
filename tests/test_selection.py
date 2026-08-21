from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from epub_news_feeder.selection import (
    AncestorBudget,
    BriefCandidate,
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
@pytest.mark.property
def test_ticket_07_budget_feasible_section_minima_precede_weighted_redistribution() -> None:
    main_budget = AncestorBudget("main", max_articles=2)
    high_weight = SectionRequest(
        "high-weight",
        "High weight",
        0,
        Policy.COVERAGE,
        3,
        min_articles=1,
        weight=10,
        candidates=(
            candidate("high-1", "a", hours_old=0),
            candidate("high-2", "a", hours_old=1),
            candidate("high-3", "a", hours_old=2),
        ),
        ancestor_budgets=(main_budget,),
    )
    low_weight = SectionRequest(
        "low-weight",
        "Low weight",
        1,
        Policy.COVERAGE,
        3,
        min_articles=1,
        weight=1,
        candidates=(
            candidate("low-1", "b", hours_old=0),
            candidate("low-2", "b", hours_old=1),
        ),
        ancestor_budgets=(main_budget,),
    )

    result = select_publication(
        PublicationRequest(max_articles=4, min_articles=1, sections=(high_weight, low_weight))
    )

    assert [slot.article.article_id for slot in result.for_section("high-weight")] == ["high-1"]
    assert [slot.article.article_id for slot in result.for_section("low-weight")] == ["low-1"]
    assert len(result.unique_article_ids) == 2


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
@pytest.mark.property
def test_ticket_08_interest_one_slot_discovery_selects_unscored_coverage() -> None:
    interests = SectionRequest(
        "interests",
        "Interests",
        0,
        Policy.INTEREST,
        1,
        discovery_percent=0.2,
        candidates=(
            candidate("preferred", "a", hours_old=0, interest=10),
            candidate("discovery", "b", hours_old=8, interest=0),
        ),
    )

    result = select_publication(
        PublicationRequest(max_articles=1, min_articles=1, sections=(interests,))
    )

    assert [slot.article.article_id for slot in result.for_section("interests")] == ["discovery"]


@pytest.mark.acceptance
def test_ticket_07_plurality_values_are_configurable_per_section() -> None:
    unconstrained = SectionRequest(
        "unconstrained",
        "Unconstrained",
        0,
        Policy.COVERAGE,
        3,
        minimum_sources=1,
        single_source_cap=1.0,
        candidates=(
            candidate("a-essential-1", "a", hours_old=0, essential=True),
            candidate("a-essential-2", "a", hours_old=1, essential=True),
            candidate("b", "b", hours_old=2),
        ),
    )

    result = select_publication(
        PublicationRequest(max_articles=3, min_articles=1, sections=(unconstrained,))
    )

    assert len(result.for_section("unconstrained")) == 3
    assert result.warnings == ()


def test_interest_source_cap_substitutes_other_sources_instead_of_relaxing() -> None:
    """Observed live: one Section ran three-for-three from a single flooding source two
    mornings in one week. The interest ordering truncated to the slot count before the
    plurality cap ran, so the cap could only relax back into the flood — it could never
    pull another source's candidate in, because that candidate was already cut."""

    section = SectionRequest(
        section_id="david",
        title="David",
        order=0,
        policy=Policy.INTEREST,
        max_articles=3,
        discovery_percent=0.0,
        minimum_sources=1,
        single_source_cap=0.4,
        candidates=(
            candidate("flood-1", "csn", hours_old=0),
            candidate("flood-2", "csn", hours_old=1),
            candidate("flood-3", "csn", hours_old=2),
            candidate("ars-1", "ars", hours_old=6),
            candidate("hackaday-1", "hackaday", hours_old=9),
        ),
    )

    result = select_publication(
        PublicationRequest(max_articles=3, min_articles=1, sections=(section,))
    )

    assert {slot.article.source_id for slot in result.for_section("david")} == {
        "csn",
        "ars",
        "hackaday",
    }


def test_interest_source_cap_still_relaxes_when_only_one_source_has_candidates() -> None:
    section = SectionRequest(
        section_id="david",
        title="David",
        order=0,
        policy=Policy.INTEREST,
        max_articles=3,
        discovery_percent=0.0,
        minimum_sources=1,
        single_source_cap=0.4,
        candidates=(
            candidate("flood-1", "csn", hours_old=0),
            candidate("flood-2", "csn", hours_old=1),
            candidate("flood-3", "csn", hours_old=2),
        ),
    )

    result = select_publication(
        PublicationRequest(max_articles=3, min_articles=1, sections=(section,))
    )

    assert len(result.for_section("david")) == 3
    assert "SOURCE_PLURALITY_RELAXED:david" in result.warnings


@pytest.mark.acceptance
@pytest.mark.property
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


@pytest.mark.property
def test_ticket_09_identity_is_unique_within_a_section_and_once_per_ancestor() -> None:
    shared_a = candidate("shared", "source-a", hours_old=0, relevance=3)
    shared_b = candidate("shared", "source-b", hours_old=0, relevance=2)
    main_budget = AncestorBudget("main", max_articles=1)
    result = select_publication(
        PublicationRequest(
            max_articles=1,
            min_articles=1,
            sections=(
                SectionRequest(
                    "technology",
                    "Technology",
                    0,
                    Policy.COVERAGE,
                    2,
                    candidates=(shared_a, shared_b),
                    ancestor_budgets=(main_budget,),
                ),
                SectionRequest(
                    "world",
                    "World",
                    1,
                    Policy.COVERAGE,
                    2,
                    candidates=(shared_a,),
                    ancestor_budgets=(main_budget,),
                ),
            ),
        )
    )

    assert result.unique_article_ids == ("shared",)
    assert len(result.for_section("technology")) == 1
    assert len(result.for_section("world")) == 1


@pytest.mark.acceptance
@pytest.mark.property
def test_ticket_10_cluster_round_robin_precedes_another_perspective() -> None:
    now = datetime(2026, 8, 9, 6, tzinfo=UTC)
    section = SectionRequest(
        "world",
        "World",
        0,
        Policy.COVERAGE,
        3,
        minimum_sources=1,
        candidates=(
            SectionCandidate(Candidate("a1", "a", "A1", "https://a/1", now, cluster_id="story-a")),
            SectionCandidate(Candidate("a2", "a", "A2", "https://a/2", now, cluster_id="story-a")),
            SectionCandidate(Candidate("b1", "a", "B1", "https://a/3", now, cluster_id="story-b")),
        ),
    )

    result = select_publication(PublicationRequest(3, 1, (section,)))

    assert [slot.article.article_id for slot in result.slots] == ["a1", "b1", "a2"]


def brief(
    brief_id: str,
    source_id: str,
    *,
    hours_old: int,
    muted: bool = False,
) -> BriefCandidate:
    return BriefCandidate(
        brief_id=brief_id,
        source_id=source_id,
        title=f"Brief {brief_id}",
        canonical_url=f"https://{source_id}.example/{brief_id}",
        published_at=datetime(2026, 8, 9, 6, tzinfo=UTC) - timedelta(hours=hours_old),
        muted=muted,
    )


def _brief_request(
    briefs: tuple[BriefCandidate, ...],
    *,
    max_briefs: int = 6,
    max_articles: int = 4,
    min_articles: int = 1,
    candidates: tuple[SectionCandidate, ...] = (),
) -> PublicationRequest:
    return PublicationRequest(
        max_articles=max_articles,
        min_articles=min_articles,
        sections=(
            SectionRequest(
                section_id="current",
                title="Current reporting",
                order=0,
                policy=Policy.COVERAGE,
                max_articles=max_articles,
                candidates=candidates,
            ),
        ),
        max_briefs=max_briefs,
        briefs=briefs,
    )


def test_briefs_never_consume_an_article_slot() -> None:
    articles = tuple(candidate(f"a-{index}", "source-a", hours_old=index) for index in range(4))
    briefs = tuple(brief(f"b-{index}", "radio", hours_old=index) for index in range(3))

    result = select_publication(_brief_request(briefs, max_articles=4, candidates=articles))

    # The Article budget is full, and the Briefs arrive anyway.
    assert len(result.unique_article_ids) == 4
    assert len(result.selected_briefs) == 3
    assert not set(result.unique_article_ids) & {item.brief_id for item in result.selected_briefs}


def test_briefs_do_not_satisfy_the_publication_minimum() -> None:
    briefs = tuple(brief(f"b-{index}", "radio", hours_old=index) for index in range(6))

    result = select_publication(_brief_request(briefs, min_articles=1, candidates=()))

    assert result.selected_briefs
    assert not result.meets_minimum


def test_brief_cap_is_independent_of_the_article_budget() -> None:
    briefs = tuple(brief(f"b-{index}", "radio", hours_old=index) for index in range(10))

    result = select_publication(_brief_request(briefs, max_briefs=6, max_articles=2))

    assert len(result.selected_briefs) == 6


def test_muted_briefs_are_never_selected() -> None:
    briefs = (
        brief("kept", "radio", hours_old=1),
        brief("muted", "radio", hours_old=0, muted=True),
    )

    result = select_publication(_brief_request(briefs))

    assert [item.brief_id for item in result.selected_briefs] == ["kept"]


def test_briefs_are_taken_round_robin_so_one_source_cannot_fill_the_roll() -> None:
    # Every "loud" Brief is newer than every "quiet" one; a purely chronological take would
    # select only "loud".
    briefs = tuple(brief(f"loud-{index}", "loud", hours_old=index) for index in range(6)) + tuple(
        brief(f"quiet-{index}", "quiet", hours_old=20 + index) for index in range(6)
    )

    result = select_publication(_brief_request(briefs, max_briefs=4))

    sources = [item.source_id for item in result.selected_briefs]
    assert sorted(sources) == ["loud", "loud", "quiet", "quiet"]


def test_selected_briefs_are_presented_newest_first_across_all_sources() -> None:
    briefs = (
        brief("quiet-newest", "quiet", hours_old=1),
        brief("loud-newest", "loud", hours_old=0),
        brief("quiet-older", "quiet", hours_old=3),
        brief("loud-older", "loud", hours_old=2),
    )

    result = select_publication(_brief_request(briefs, max_briefs=4))

    assert [item.brief_id for item in result.selected_briefs] == [
        "loud-newest",
        "quiet-newest",
        "loud-older",
        "quiet-older",
    ]


def test_brief_presentation_order_breaks_ties_deterministically() -> None:
    same_moment = tuple(
        BriefCandidate(
            brief_id=f"brief-{index}",
            source_id=source_id,
            title="Same moment",
            canonical_url=url,
            published_at=datetime(2026, 8, 9, 6, tzinfo=UTC),
        )
        for index, (source_id, url) in enumerate(
            (
                ("zulu", "https://zulu.example/b"),
                ("alpha", "https://alpha.example/z"),
                ("alpha", "https://alpha.example/a"),
            )
        )
    )

    first = select_publication(_brief_request(same_moment))
    second = select_publication(_brief_request(tuple(reversed(same_moment))))

    order = [(item.source_id, item.canonical_url) for item in first.selected_briefs]
    assert order == [
        ("alpha", "https://alpha.example/a"),
        ("alpha", "https://alpha.example/z"),
        ("zulu", "https://zulu.example/b"),
    ]
    assert [(item.source_id, item.canonical_url) for item in second.selected_briefs] == order


def test_a_publication_without_briefs_selects_none() -> None:
    result = select_publication(
        _brief_request((), candidates=(candidate("a-1", "source-a", hours_old=1),))
    )

    assert result.selected_briefs == ()
