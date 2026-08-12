"""One Publication reading another's delivery history.

Suppression is per-Publication, and that boundary is load-bearing. These tests cover the one
sanctioned way through it: a named, one-directional reference that lets the Saturday Edition
know what the weekdays already delivered, so it completes the week instead of reprinting it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from epub_news_feeder.models import Configuration
from epub_news_feeder.selection import (
    Candidate,
    Policy,
    PublicationRequest,
    SectionCandidate,
    SectionRequest,
    select_publication,
)
from epub_news_feeder.state import StateStore

MONDAY = datetime(2026, 8, 10, 6, tzinfo=UTC)


def _configuration(publications: list[dict[str, object]]) -> Configuration:
    return Configuration.model_validate(
        {
            "version": 1,
            "sources": {
                "source-one": {
                    "title": "Source one",
                    "feed_url": "https://publisher.example/feed.xml",
                }
            },
            "publications": publications,
        }
    )


def _publication(publication_id: str, **extra: object) -> dict[str, object]:
    return {
        "id": publication_id,
        "title": publication_id.title(),
        "sections": [
            {"id": "section-one", "title": "Section one", "sources": ["source-one"]},
        ],
        **extra,
    }


def test_a_publication_may_name_another_declared_later_in_the_file() -> None:
    """Order in a configuration file is presentation, not dependency."""

    configuration = _configuration(
        [
            _publication("weekly", reads_history_from=["daily"]),
            _publication("daily"),
        ]
    )
    weekly = next(item for item in configuration.publications if item.id == "weekly")
    assert weekly.reads_history_from == ["daily"]


def test_every_publication_reads_nobody_by_default() -> None:
    """The field exists for one Publication. Every other Edition must be unchanged by it."""

    configuration = _configuration([_publication("daily")])
    assert configuration.publications[0].reads_history_from == []


@pytest.mark.parametrize(
    ("publications", "message"),
    [
        pytest.param(
            [_publication("daily", reads_history_from=["daily"])],
            "reads its own delivery history",
            id="self-reference",
        ),
        pytest.param(
            [_publication("weekly", reads_history_from=["nonexistent"])],
            "unknown publication's history",
            id="unknown-publication",
        ),
        pytest.param(
            [_publication("weekly", reads_history_from=["daily", "daily"]), _publication("daily")],
            "duplicate publication history reference",
            id="duplicate-reference",
        ),
        pytest.param(
            [
                _publication("weekly", reads_history_from=["daily"]),
                _publication("daily", reads_history_from=["weekly"]),
            ],
            "form a cycle",
            id="two-publication-cycle",
        ),
        pytest.param(
            [
                _publication("weekly", reads_history_from=["daily"]),
                _publication("daily", reads_history_from=["monthly"]),
                _publication("monthly", reads_history_from=["weekly"]),
            ],
            "form a cycle",
            id="three-publication-cycle",
        ),
    ],
)
def test_incoherent_history_references_are_rejected(
    publications: list[dict[str, object]], message: str
) -> None:
    """A cycle has no defensible reading: whichever ran first would decide what the other carried.

    Rejecting these at load is cheaper than explaining the resulting Edition, and a scheduled
    Publication's configuration error must surface in the gate rather than at dawn.
    """

    with pytest.raises(ValidationError, match=message):
        _configuration(publications)


def _deliver(state: StateStore, *, publication_id: str, url: str, when: datetime) -> str:
    """Observe one Article and record it as delivered by *publication_id*."""

    observed = state.observe_article(
        source_id="source",
        publisher_id="publisher",
        canonical_url=url,
        guid=url,
        title="Article",
        author=None,
        normalized_body=f"complete article body about {url} " * 40,
        observed_at=when,
    )
    run_id = f"run-{publication_id}-{url.rsplit('/', 1)[-1]}"
    state.begin_run(run_id, publication_id, f"edition-{run_id}", when)
    state.reserve_articles(
        run_id,
        publication_id,
        [observed],
        when + timedelta(hours=1),
        article_count=1,
        publisher_link_count=0,
    )
    state.finalize_delivery(run_id, publication_id, when, f"digest-{run_id}")
    return observed.article_id


def test_delivered_article_ids_answers_for_exactly_the_publications_named(tmp_path: Path) -> None:
    """The weekly asks what the daily delivered, and must not be told what anyone else did."""

    with StateStore(tmp_path / "state.sqlite3", environment="test") as state:
        from_daily = _deliver(
            state, publication_id="daily", url="https://publisher.example/a", when=MONDAY
        )
        from_household = _deliver(
            state,
            publication_id="household",
            url="https://publisher.example/b",
            when=MONDAY + timedelta(days=1),
        )

        assert state.delivered_article_ids(("daily",)) == frozenset({from_daily})
        assert state.delivered_article_ids(("daily", "household")) == frozenset(
            {from_daily, from_household}
        )
        # A Publication naming nothing is the overwhelming majority, and owes nobody attention.
        assert state.delivered_article_ids(()) == frozenset()
        # A named Publication that has never run is the fresh-install case, and is not an error.
        assert state.delivered_article_ids(("weekly",)) == frozenset()
        # Repeated identifiers are a configuration smell, not a double count.
        assert state.delivered_article_ids(("daily", "daily")) == frozenset({from_daily})


def test_cluster_recurrence_counts_distinct_days_rather_than_articles(tmp_path: Path) -> None:
    """Recurrence is what the weekly leads with, so it must measure returning, not volume.

    Four publishers covering one story on one morning is a busy morning. One publisher returning
    to a story across four mornings is the week's continuing thread. Counting Articles would rank
    those identically and get the Saturday Edition precisely backwards.
    """

    with StateStore(tmp_path / "state.sqlite3", environment="test") as state:

        def cluster_of(slug: str, signals: list[str], days: list[int]) -> str:
            """Cluster `len(days)` real Articles on shared signals, delivering one per day."""

            article_ids: list[str] = []
            for index, _ in enumerate(days):
                url = f"https://publisher.example/{slug}-{index}"
                observed = state.observe_article(
                    source_id="source",
                    publisher_id="publisher",
                    canonical_url=url,
                    guid=url,
                    title=f"{slug} {index}",
                    author=None,
                    normalized_body=f"complete article body about {slug} {index} " * 40,
                    observed_at=MONDAY,
                )
                article_ids.append(observed.article_id)
                state.match_story_cluster(observed.article_id, signals=signals, observed_at=MONDAY)
            cluster_id = state.story_cluster(article_ids[0])
            assert cluster_id is not None, f"{slug} did not cluster on shared signals"
            for article_id, day in zip(article_ids, days, strict=True):
                state.record_cluster_delivery(
                    publication_id="daily",
                    cluster_id=cluster_id,
                    article_id=article_id,
                    title=slug,
                    publisher_id="publisher",
                    canonical_url=f"https://publisher.example/{slug}-{article_id}",
                    publisher_published_at=MONDAY,
                    delivered_at=MONDAY + timedelta(days=day),
                )
            return cluster_id

        # Three Articles, one morning: a busy day, which recurred once.
        busy = cluster_of("busy", ["election", "results", "parliament"], [0, 0, 0])
        # Three Articles, three mornings: the week's continuing thread.
        thread = cluster_of("thread", ["wildfire", "evacuation", "drought"], [0, 1, 2])

        assert state.cluster_recurrence(("daily",)) == {busy: 1, thread: 3}
        # The window is what keeps coverage that is never pruned from outranking the week.
        assert state.cluster_recurrence(("daily",), since=MONDAY + timedelta(days=2)) == {thread: 1}
        # Nothing was asked about, so nothing is answered - the fail-open path.
        assert state.cluster_recurrence(()) == {}
        assert state.cluster_recurrence(("household",)) == {}


def _candidate(article_id: str, *, cluster_id: str, recurrence: int) -> SectionCandidate:
    return SectionCandidate(
        Candidate(
            article_id,
            "source-one",
            f"Article {article_id}",
            f"https://publisher.example/{article_id}",
            MONDAY,
            cluster_id=cluster_id,
        ),
        recurrence=recurrence,
    )


def test_recurrence_leads_the_ordering_without_monopolising_the_section() -> None:
    """Leading the Section is the requirement; filling it is not.

    The continuing thread should open the Section, and then cluster diversification should hand
    the next slot to a different story. A Saturday Edition that ran four articles about one
    subject before mentioning anything else would be a worse read than the daily it completes.
    """

    result = select_publication(
        PublicationRequest(
            max_articles=4,
            min_articles=1,
            sections=(
                SectionRequest(
                    "section-one",
                    "Section one",
                    0,
                    Policy.COVERAGE,
                    max_articles=4,
                    minimum_sources=1,
                    single_source_cap=1.0,
                    candidates=(
                        _candidate("quiet-a", cluster_id="quiet", recurrence=0),
                        _candidate("thread-a", cluster_id="thread", recurrence=4),
                        _candidate("thread-b", cluster_id="thread", recurrence=4),
                        _candidate("quiet-b", cluster_id="quiet", recurrence=0),
                    ),
                ),
            ),
        )
    )
    ordered = [slot.article.article_id for slot in result.slots]

    assert ordered[0].startswith("thread"), "the week's continuing thread must lead"
    assert not ordered[1].startswith("thread"), "and must not take the next slot as well"


def test_essential_coverage_still_outranks_the_weeks_most_recurrent_story() -> None:
    """Recurrence is a promotion layered over the policy, not a licence to displace essentials.

    A Section that declares essential coverage is stating a floor for what the Edition owes the
    reader. A story the daily happened to return to four times does not get to push that below
    the fold, however much of the week it occupied.
    """

    essential = SectionCandidate(
        Candidate(
            "essential",
            "source-one",
            "Essential",
            "https://publisher.example/essential",
            MONDAY,
            cluster_id="quiet",
        ),
        essential=True,
        recurrence=0,
    )
    recurrent = _candidate("recurrent", cluster_id="thread", recurrence=9)

    result = select_publication(
        PublicationRequest(
            max_articles=2,
            min_articles=1,
            sections=(
                SectionRequest(
                    "section-one",
                    "Section one",
                    0,
                    Policy.COVERAGE,
                    max_articles=2,
                    minimum_sources=1,
                    single_source_cap=1.0,
                    candidates=(recurrent, essential),
                ),
            ),
        )
    )

    assert [slot.article.article_id for slot in result.slots] == ["essential", "recurrent"]
