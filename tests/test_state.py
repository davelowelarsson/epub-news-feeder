from __future__ import annotations

import hashlib
import sqlite3
import stat
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from epub_news_feeder.state import StateStore, brief_id, normalize_url


@pytest.mark.acceptance
def test_ticket_06_state_store_enforces_writer_lock_permissions_and_keyed_fingerprints(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "private-state"
    state_path = state_dir / "test.sqlite3"
    observed_at = datetime(2026, 8, 9, 6, tzinfo=UTC)
    known_unkeyed_token_hash = hashlib.sha256(b"epub-news-feeder:v1:dictionary-token").hexdigest()

    with StateStore(state_path, environment="test") as state:
        state.observe_article(
            source_id="source",
            publisher_id="publisher",
            canonical_url="https://publisher.example/private",
            guid="private",
            title="Private",
            author=None,
            normalized_body="dictionary-token " * 100,
            observed_at=observed_at,
        )
        with (
            pytest.raises(RuntimeError, match="already open for writing"),
            StateStore(state_path, environment="test"),
        ):
            pass

    key_path = state_path.with_suffix(f"{state_path.suffix}.key")
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert known_unkeyed_token_hash.encode() not in state_path.read_bytes()
    assert b"dictionary-token" not in state_path.read_bytes()

    second_path = state_dir / "second.sqlite3"
    with StateStore(second_path, environment="second") as second:
        second.observe_article(
            source_id="source",
            publisher_id="publisher",
            canonical_url="https://publisher.example/private",
            guid="private",
            title="Private",
            author=None,
            normalized_body="dictionary-token " * 100,
            observed_at=observed_at,
        )
    assert key_path.read_bytes() != second_path.with_suffix(".sqlite3.key").read_bytes()


@pytest.mark.acceptance
def test_ticket_06_reservations_expire_and_same_run_operations_are_idempotent(
    tmp_path: Path,
) -> None:
    observed_at = datetime(2026, 8, 9, 6, tzinfo=UTC)
    with StateStore(tmp_path / "state.sqlite3", environment="test") as state:
        observed = state.observe_article(
            source_id="source",
            publisher_id="publisher",
            canonical_url="https://publisher.example/article",
            guid="article",
            title="Article",
            author=None,
            normalized_body="complete article body " * 100,
            observed_at=observed_at,
        )
        state.begin_run("run", "daily", "edition", observed_at)
        state.begin_run("run", "daily", "edition", observed_at)
        state.reserve_articles(
            "run",
            "daily",
            [observed],
            observed_at + timedelta(hours=1),
            article_count=1,
            publisher_link_count=2,
        )
        state.reserve_articles(
            "run",
            "daily",
            [observed],
            observed_at + timedelta(hours=1),
            article_count=1,
            publisher_link_count=2,
        )
        assert state.run_item_counts("run") == (1, 2)
        with pytest.raises(RuntimeError, match="item counts are immutable"):
            state.reserve_articles(
                "run",
                "daily",
                [observed],
                observed_at + timedelta(hours=1),
                article_count=2,
                publisher_link_count=1,
            )

        assert state.active_reservations("daily", as_of=observed_at) == [observed.article_id]
        blocked = state.observe_article(
            source_id="source",
            publisher_id="publisher",
            canonical_url="https://publisher.example/article",
            guid="article",
            title="Article",
            author=None,
            normalized_body="complete article body " * 100,
            observed_at=observed_at + timedelta(minutes=30),
            publication_id="daily",
        )
        assert not blocked.eligible
        resumable = state.observe_article(
            source_id="source",
            publisher_id="publisher",
            canonical_url="https://publisher.example/article",
            guid="article",
            title="Article",
            author=None,
            normalized_body="complete article body " * 100,
            observed_at=observed_at + timedelta(minutes=30),
            publication_id="daily",
            run_id="run",
        )
        assert resumable.eligible
        assert state.cleanup_expired_reservations(observed_at + timedelta(hours=2)) == 1
        assert state.active_reservations("daily") == []


@pytest.mark.acceptance
def test_ticket_06_pending_delivery_is_immutable_idempotent_and_cleared_on_finalize(
    tmp_path: Path,
) -> None:
    observed_at = datetime(2026, 8, 9, 6, tzinfo=UTC)
    with StateStore(tmp_path / "state.sqlite3", environment="test") as state:
        observed = state.observe_article(
            source_id="source",
            publisher_id="publisher",
            canonical_url="https://publisher.example/article",
            guid="article",
            title="Article",
            author=None,
            normalized_body="complete article body " * 100,
            observed_at=observed_at,
        )
        state.begin_run("run", "daily", "edition", observed_at)
        state.reserve_articles("run", "daily", [observed], observed_at + timedelta(hours=1))
        pending = state.prepare_delivery(
            run_id="run",
            publication_id="daily",
            delivery_target="local:/editions/daily.epub",
            delivery_digest="immutable-digest",
            prepared_at=observed_at + timedelta(minutes=10),
        )
        retried = state.prepare_delivery(
            run_id="run",
            publication_id="daily",
            delivery_target="local:/editions/daily.epub",
            delivery_digest="immutable-digest",
            prepared_at=observed_at + timedelta(minutes=10),
        )

        assert retried == pending
        assert state.pending_deliveries("daily") == [pending]
        with pytest.raises(RuntimeError, match="immutable"):
            state.prepare_delivery(
                run_id="run",
                publication_id="daily",
                delivery_target="local:/editions/daily.epub",
                delivery_digest="different-digest",
                prepared_at=observed_at + timedelta(minutes=10),
            )

        state.finalize_delivery(
            "run", "daily", observed_at + timedelta(minutes=20), "immutable-digest"
        )
        state.finalize_delivery(
            "run", "daily", observed_at + timedelta(minutes=20), "immutable-digest"
        )
        assert state.pending_deliveries("daily") == []


@pytest.mark.acceptance
@pytest.mark.property
def test_ticket_06_identity_collapses_exact_body_and_keeps_all_provenance(
    tmp_path: Path,
) -> None:
    observed_at = datetime(2026, 8, 9, 6, tzinfo=UTC)
    body = "  The publisher's exact article body.  " * 80

    with StateStore(tmp_path / "state.sqlite3", environment="test") as state:
        original = state.observe_article(
            source_id="homepage",
            publisher_id="publisher-a",
            canonical_url="https://a.example/report?utm_source=home",
            guid="a-guid",
            title="Original",
            author="Reporter A",
            normalized_body=body,
            observed_at=observed_at,
        )
        syndicated = state.observe_article(
            source_id="wire-feed",
            publisher_id="publisher-b",
            canonical_url="https://b.example/syndicated-report",
            guid="b-guid",
            title="Syndicated title",
            author="Reporter A",
            normalized_body="The publisher's exact article body. " * 80,
            observed_at=observed_at + timedelta(minutes=5),
        )

        assert syndicated.article_id == original.article_id
        assert state.discovery_provenance(original.article_id) == [
            {
                "source_id": "homepage",
                "publisher_id": "publisher-a",
                "url": "https://a.example/report",
                "guid": "a-guid",
                "discovered_at": "2026-08-09T06:00:00+00:00",
            },
            {
                "source_id": "wire-feed",
                "publisher_id": "publisher-b",
                "url": "https://b.example/syndicated-report",
                "guid": "b-guid",
                "discovered_at": "2026-08-09T06:05:00+00:00",
            },
        ]


@pytest.mark.acceptance
@pytest.mark.property
def test_ticket_06_identity_near_duplicate_is_same_publisher_and_window_bounded(
    tmp_path: Path,
) -> None:
    observed_at = datetime(2026, 8, 9, 6, tzinfo=UTC)
    original_body = " ".join(f"word-{index}" for index in range(200))
    near_body = original_body.replace("word-199", "corrected-word")

    with StateStore(
        tmp_path / "state.sqlite3",
        environment="test",
        near_duplicate_similarity=0.99,
        near_duplicate_window=timedelta(hours=12),
    ) as state:
        original = state.observe_article(
            source_id="feed-a",
            publisher_id="publisher-a",
            canonical_url="https://a.example/first",
            guid="first",
            title="A report",
            author=None,
            normalized_body=original_body,
            observed_at=observed_at,
        )
        same_publisher = state.observe_article(
            source_id="feed-a",
            publisher_id="publisher-a",
            canonical_url="https://a.example/copy",
            guid="copy",
            title="A report (updated URL)",
            author=None,
            normalized_body=near_body,
            observed_at=observed_at + timedelta(hours=1),
        )
        other_publisher = state.observe_article(
            source_id="feed-b",
            publisher_id="publisher-b",
            canonical_url="https://b.example/coverage",
            guid="coverage",
            title="Independent coverage",
            author=None,
            normalized_body=near_body.replace("word-198", "independent-word"),
            observed_at=observed_at + timedelta(hours=2),
        )
        outside_window = state.observe_article(
            source_id="feed-a",
            publisher_id="publisher-a",
            canonical_url="https://a.example/later-copy",
            guid="later-copy",
            title="Later reuse",
            author=None,
            normalized_body=near_body.replace("word-198", "another-correction"),
            observed_at=observed_at + timedelta(days=2),
        )

        assert same_publisher.article_id == original.article_id
        assert other_publisher.article_id != original.article_id
        assert outside_window.article_id != original.article_id


@pytest.mark.acceptance
@pytest.mark.property
def test_ticket_10_revision_accumulates_against_delivery_and_suppresses_reversion(
    tmp_path: Path,
) -> None:
    observed_at = datetime(2026, 8, 9, 6, tzinfo=UTC)
    original_words = [f"word-{index}" for index in range(400)]

    with StateStore(tmp_path / "state.sqlite3", environment="test") as state:
        original = state.observe_article(
            source_id="source",
            publisher_id="publisher",
            canonical_url="https://publisher.example/article",
            guid="article",
            title="Article",
            author=None,
            normalized_body=" ".join(original_words),
            observed_at=observed_at,
            publication_id="daily",
        )
        state.begin_run("original-run", "daily", "original-edition", observed_at)
        state.reserve_articles(
            "original-run", "daily", [original], observed_at + timedelta(hours=1)
        )
        state.finalize_delivery("original-run", "daily", observed_at, "original-digest")

        first_edit_words = [*original_words]
        first_edit_words[:30] = [f"first-edit-{index}" for index in range(30)]
        first_edit = state.observe_article(
            source_id="source",
            publisher_id="publisher",
            canonical_url="https://publisher.example/article",
            guid="article",
            title="Article",
            author=None,
            normalized_body=" ".join(first_edit_words),
            observed_at=observed_at + timedelta(hours=1),
            publication_id="daily",
        )
        assert not first_edit.eligible
        assert not first_edit.materially_changed

        accumulated_words = [*first_edit_words]
        accumulated_words[30:60] = [f"second-edit-{index}" for index in range(30, 60)]
        accumulated = state.observe_article(
            source_id="source",
            publisher_id="publisher",
            canonical_url="https://publisher.example/article",
            guid="article",
            title="Article",
            author=None,
            normalized_body=" ".join(accumulated_words),
            observed_at=observed_at + timedelta(hours=2),
            publication_id="daily",
        )
        assert accumulated.eligible
        assert accumulated.materially_changed

        reverted = state.observe_article(
            source_id="source",
            publisher_id="publisher",
            canonical_url="https://publisher.example/article",
            guid="article",
            title="Article",
            author=None,
            normalized_body=" ".join(original_words),
            observed_at=observed_at + timedelta(hours=3),
            publication_id="daily",
        )
        assert not reverted.eligible
        assert not reverted.materially_changed


@pytest.mark.acceptance
@pytest.mark.property
def test_ticket_10_revision_correction_queues_independently_until_acknowledged(
    tmp_path: Path,
) -> None:
    observed_at = datetime(2026, 8, 9, 6, tzinfo=UTC)
    original_words = [f"word-{index}" for index in range(400)]

    with StateStore(tmp_path / "state.sqlite3", environment="test") as state:
        original = state.observe_article(
            source_id="source",
            publisher_id="publisher",
            canonical_url="https://publisher.example/article",
            guid="article",
            title="Article",
            author=None,
            normalized_body=" ".join(original_words),
            observed_at=observed_at,
            publication_id="daily",
        )
        state.begin_run("original-run", "daily", "original-edition", observed_at)
        state.reserve_articles(
            "original-run", "daily", [original], observed_at + timedelta(hours=1)
        )
        state.finalize_delivery("original-run", "daily", observed_at, "original-digest")

        corrected_words = [*original_words]
        corrected_words[0] = "corrected-word"
        corrected = state.observe_article(
            source_id="source",
            publisher_id="publisher",
            canonical_url="https://publisher.example/article",
            guid="article",
            title="Article",
            author=None,
            normalized_body=" ".join(corrected_words),
            observed_at=observed_at + timedelta(hours=1),
            publication_id="daily",
            correction_signal_id="publisher-notice-42",
            correction_kind="correction",
        )

        assert corrected.eligible
        assert not corrected.materially_changed
        assert corrected.correction_pending
        pending = state.pending_corrections("daily")
        assert [(item.signal_id, item.article_id, item.kind) for item in pending] == [
            ("publisher-notice-42", original.article_id, "correction")
        ]
        assert state.active_reservations("daily") == []

        state.acknowledge_corrections(
            "daily",
            ["publisher-notice-42"],
            delivered_at=observed_at + timedelta(hours=2),
        )
        assert state.pending_corrections("daily") == []


@pytest.mark.acceptance
def test_ticket_10_story_cluster_requires_two_signals_and_is_deterministic(
    tmp_path: Path,
) -> None:
    observed_at = datetime(2026, 8, 9, 6, tzinfo=UTC)

    with StateStore(
        tmp_path / "state.sqlite3",
        environment="test",
        story_cluster_window=timedelta(days=3),
        story_cluster_min_signals=2,
    ) as state:
        first = state.observe_article(
            source_id="source-a",
            publisher_id="publisher-a",
            canonical_url="https://a.example/harbor",
            guid="harbor",
            title="Harbor closure",
            author=None,
            normalized_body="first distinct body",
            observed_at=observed_at,
        )
        uncertain = state.observe_article(
            source_id="source-b",
            publisher_id="publisher-b",
            canonical_url="https://b.example/weather",
            guid="weather",
            title="Regional weather",
            author=None,
            normalized_body="second distinct body",
            observed_at=observed_at + timedelta(hours=1),
        )
        continuing = state.observe_article(
            source_id="source-c",
            publisher_id="publisher-c",
            canonical_url="https://c.example/harbor-response",
            guid="response",
            title="Harbor response",
            author=None,
            normalized_body="third distinct body",
            observed_at=observed_at + timedelta(hours=2),
        )

        assert (
            state.match_story_cluster(
                first.article_id,
                signals=["place:North Harbor", "event:closure", "actor:port-authority"],
                observed_at=observed_at,
            )
            is None
        )
        assert (
            state.match_story_cluster(
                uncertain.article_id,
                signals=["place:North Harbor", "event:storm"],
                observed_at=observed_at + timedelta(hours=1),
            )
            is None
        )
        cluster_id = state.match_story_cluster(
            continuing.article_id,
            signals=["event:closure", "actor:port-authority", "impact:ferries"],
            observed_at=observed_at + timedelta(hours=2),
        )

        expected_id = state.deterministic_cluster_id(first.article_id, continuing.article_id)
        assert cluster_id == expected_id
        assert state.story_cluster(first.article_id) == expected_id
        assert state.story_cluster(continuing.article_id) == expected_id
        assert state.story_cluster(uncertain.article_id) is None
        assert state.story_cluster_members(expected_id) == [first.article_id, continuing.article_id]


@pytest.mark.acceptance
def test_ticket_10_story_cluster_overrides_keep_body_free_prior_metadata(
    tmp_path: Path,
) -> None:
    observed_at = datetime(2026, 8, 9, 6, tzinfo=UTC)

    with StateStore(tmp_path / "state.sqlite3", environment="test") as state:
        first = state.observe_article(
            source_id="source-a",
            publisher_id="publisher-a",
            canonical_url="https://a.example/first",
            guid="first",
            title="First coverage",
            author=None,
            normalized_body="old article body that must never enter cluster history",
            observed_at=observed_at,
        )
        second = state.observe_article(
            source_id="source-b",
            publisher_id="publisher-b",
            canonical_url="https://b.example/second",
            guid="second",
            title="Second coverage",
            author=None,
            normalized_body="current article body that remains distinct",
            observed_at=observed_at + timedelta(hours=1),
        )
        state.match_story_cluster(
            first.article_id,
            signals=["event:closure", "place:harbor"],
            observed_at=observed_at,
        )
        cluster_id = state.match_story_cluster(
            second.article_id,
            signals=["event:closure", "place:harbor"],
            observed_at=observed_at + timedelta(hours=1),
        )
        assert cluster_id is not None

        state.record_cluster_delivery(
            publication_id="daily",
            cluster_id=cluster_id,
            article_id=first.article_id,
            title="First coverage",
            publisher_id="publisher-a",
            canonical_url="https://a.example/first",
            publisher_published_at=observed_at - timedelta(minutes=30),
            delivered_at=observed_at + timedelta(hours=2),
        )
        # A later delivered revision must not duplicate the Article in coverage history.
        state.record_cluster_delivery(
            publication_id="daily",
            cluster_id=cluster_id,
            article_id=first.article_id,
            title="First coverage (updated)",
            publisher_id="publisher-a",
            canonical_url="https://a.example/first",
            publisher_published_at=observed_at - timedelta(minutes=30),
            delivered_at=observed_at + timedelta(hours=3),
        )
        state.set_cluster_override(
            first.article_id,
            cluster_id=None,
            reason="Operator determined the events differ",
            recorded_at=observed_at + timedelta(hours=4),
        )
        assert (
            state.match_story_cluster(
                first.article_id,
                signals=["event:closure", "place:harbor"],
                observed_at=observed_at + timedelta(hours=5),
            )
            is None
        )

        assert state.story_cluster(first.article_id) is None
        prior = state.prior_cluster_coverage("daily", cluster_id)
        assert len(prior) == 1
        assert prior[0].title == "First coverage"
        assert {field.name for field in fields(prior[0])} == {
            "article_id",
            "title",
            "publisher_id",
            "canonical_url",
            "publisher_published_at",
            "delivered_at",
        }
        history = state.cluster_override_history(first.article_id)
        assert [(item.cluster_id, item.reason) for item in history] == [
            (None, "Operator determined the events differ")
        ]

    database_bytes = (tmp_path / "state.sqlite3").read_bytes()
    assert b"old article body" not in database_bytes
    assert b"current article body" not in database_bytes


@pytest.mark.acceptance
def test_ticket_06_delivery_history_is_body_free_and_revision_aware(tmp_path: Path) -> None:
    state_path = tmp_path / "development.sqlite3"
    delivered_at = datetime(2026, 8, 9, 6, tzinfo=UTC)
    original_body = " ".join(f"original-{word}" for word in range(400))

    with StateStore(state_path, environment="development") as state:
        observed = state.observe_article(
            source_id="fixture-source",
            publisher_id="fixture-publisher",
            canonical_url="https://publisher.example/news/item",
            guid="fixture-1",
            title="Fixture Article",
            author="Reporter",
            normalized_body=original_body,
            observed_at=delivered_at,
        )
        assert observed.eligible
        assert not observed.materially_changed

        state.begin_run(
            run_id="20260809T060000Z-AAAAAAAA",
            publication_id="daily",
            edition_id="daily@2026-08-09T06:00:00Z",
            started_at=delivered_at,
        )
        state.reserve_articles(
            run_id="20260809T060000Z-AAAAAAAA",
            publication_id="daily",
            observations=[observed],
            expires_at=delivered_at + timedelta(hours=24),
        )
        state.finalize_delivery(
            run_id="20260809T060000Z-AAAAAAAA",
            publication_id="daily",
            delivered_at=delivered_at,
            delivery_digest="abc123",
        )

        unchanged = state.observe_article(
            source_id="fixture-source",
            publisher_id="fixture-publisher",
            canonical_url="https://publisher.example/news/item?utm_source=feed",
            guid="fixture-1-alias",
            title="Fixture Article",
            author="Reporter",
            normalized_body=original_body,
            observed_at=delivered_at + timedelta(hours=1),
            publication_id="daily",
        )
        assert unchanged.article_id == observed.article_id
        assert not unchanged.eligible

        materially_updated = state.observe_article(
            source_id="fixture-source",
            publisher_id="fixture-publisher",
            canonical_url="https://publisher.example/news/item",
            guid="fixture-1",
            title="Fixture Article — updated",
            author="Reporter",
            normalized_body=" ".join(f"replacement-{word}" for word in range(400)),
            observed_at=delivered_at + timedelta(hours=2),
            publication_id="daily",
        )
        assert materially_updated.article_id == observed.article_id
        assert materially_updated.eligible
        assert materially_updated.materially_changed

    database_bytes = state_path.read_bytes()
    assert b"original-42" not in database_bytes
    assert b"replacement-42" not in database_bytes


def test_a_body_that_shrank_by_half_is_not_a_material_update(tmp_path: Path) -> None:
    """Observed live: a delivered 855-word article came back as a 129-word paywall teaser
    plus site chrome, was counted as a big diff, and re-delivered labelled "Uppdaterad".
    A body that lost half its words is an extraction artifact, not publisher news."""

    state_path = tmp_path / "development.sqlite3"
    delivered_at = datetime(2026, 8, 19, 4, tzinfo=UTC)
    full_body = " ".join(f"reporting-{word}" for word in range(400))

    with StateStore(state_path, environment="development") as state:
        observed = state.observe_article(
            source_id="fixture-source",
            publisher_id="fixture-publisher",
            canonical_url="https://publisher.example/news/school-start",
            guid="school-1",
            title="Skolstarten",
            author="Reporter",
            normalized_body=full_body,
            observed_at=delivered_at,
        )
        state.begin_run(
            run_id="20260819T040000Z-AAAAAAAA",
            publication_id="daily",
            edition_id="daily@2026-08-19T04:00:00Z",
            started_at=delivered_at,
        )
        state.reserve_articles(
            run_id="20260819T040000Z-AAAAAAAA",
            publication_id="daily",
            observations=[observed],
            expires_at=delivered_at + timedelta(hours=24),
        )
        state.finalize_delivery(
            run_id="20260819T040000Z-AAAAAAAA",
            publication_id="daily",
            delivered_at=delivered_at,
            delivery_digest="abc123",
        )

        teaser = state.observe_article(
            source_id="fixture-source",
            publisher_id="fixture-publisher",
            canonical_url="https://publisher.example/news/school-start",
            guid="school-1",
            title="Skolstarten",
            author="Reporter",
            normalized_body=" ".join(f"teaser-{word}" for word in range(120)),
            observed_at=delivered_at + timedelta(days=2),
            publication_id="daily",
        )
        assert teaser.article_id == observed.article_id
        assert not teaser.eligible
        assert not teaser.materially_changed

        # A genuine rewrite of comparable length still comes back.
        rewritten = state.observe_article(
            source_id="fixture-source",
            publisher_id="fixture-publisher",
            canonical_url="https://publisher.example/news/school-start",
            guid="school-1",
            title="Skolstarten",
            author="Reporter",
            normalized_body=" ".join(f"rewritten-{word}" for word in range(380)),
            observed_at=delivered_at + timedelta(days=3),
            publication_id="daily",
        )
        assert rewritten.eligible
        assert rewritten.materially_changed


@pytest.mark.acceptance
def test_ticket_06_abandoning_delivery_releases_reservations(tmp_path: Path) -> None:
    now = datetime(2026, 8, 9, 6, tzinfo=UTC)
    with StateStore(tmp_path / "state.sqlite3", environment="test") as state:
        observed = state.observe_article(
            source_id="source",
            publisher_id="publisher",
            canonical_url="https://publisher.example/item",
            guid="guid",
            title="Title",
            author=None,
            normalized_body="complete article body " * 100,
            observed_at=now,
        )
        state.begin_run("run", "publication", "edition", now)
        state.reserve_articles("run", "publication", [observed], now + timedelta(hours=24))

        state.abandon_run("run", reason="DELIVERY_TERMINAL")

        assert state.active_reservations("publication") == []


def test_brief_delivery_identity_is_unkeyed_hash_of_normalized_canonical_url() -> None:
    canonical = "https://publisher.example/report?utm_source=newsletter"
    expected = hashlib.sha256(normalize_url(canonical).encode()).hexdigest()[:24]

    assert brief_id(canonical) == expected
    # Tracking noise and a trailing slash normalize away, same as an Article's identity.
    assert brief_id("https://publisher.example/report/?utm_source=other") == expected
    # A different report is a different identity.
    assert brief_id("https://publisher.example/other-report") != expected
    # Not keyed: it does not depend on the fingerprint key sidecar at all.
    assert brief_id(canonical) == hashlib.sha256(normalize_url(canonical).encode()).hexdigest()[:24]


@pytest.mark.security
def test_brief_delivery_table_stores_no_headline_or_canonical_url(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite3"
    delivered_at = datetime(2026, 8, 9, 6, tzinfo=UTC)
    canonical_url = "https://publisher.example/harbour-spill-investigation"
    headline = "Exclusive: harbour spill investigation widens"
    identity = brief_id(canonical_url)

    with StateStore(state_path, environment="test") as state:
        state.begin_run("run", "daily", "edition", delivered_at)
        state.record_brief_delivery(
            publication_id="daily",
            brief_id=identity,
            source_id="ekot",
            published_at=delivered_at,
            delivered_at=delivered_at,
            run_id="run",
        )

    with sqlite3.connect(state_path) as connection:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(brief_deliveries)")}
        assert columns == {
            "publication_id",
            "brief_id",
            "source_id",
            "published_at",
            "delivered_at",
            "run_id",
        }
        row = connection.execute(
            "SELECT publication_id, brief_id, source_id, run_id FROM brief_deliveries"
        ).fetchone()
        assert row == ("daily", identity, "ekot", "run")

    database_bytes = state_path.read_bytes()
    assert headline.encode() not in database_bytes
    assert canonical_url.encode() not in database_bytes
    assert b"harbour-spill" not in database_bytes


def test_brief_delivery_suppression_is_permanent_and_scoped_per_publication(
    tmp_path: Path,
) -> None:
    delivered_at = datetime(2026, 8, 9, 6, tzinfo=UTC)
    identity = brief_id("https://publisher.example/harbour-report")

    with StateStore(tmp_path / "state.sqlite3", environment="test") as state:
        state.begin_run("run", "daily", "edition", delivered_at)
        assert state.delivered_brief_ids("daily") == frozenset()

        state.record_brief_delivery(
            publication_id="daily",
            brief_id=identity,
            source_id="ekot",
            published_at=delivered_at,
            delivered_at=delivered_at,
            run_id="run",
        )

        assert state.delivered_brief_ids("daily") == frozenset({identity})
        # Scoped per Publication: delivery to "daily" leaves "weekend" untouched.
        assert state.delivered_brief_ids("weekend") == frozenset()

        # Permanent: recording again (e.g. a resumed Run recovering its own staged Brief)
        # neither raises nor prunes, and the identity remains suppressed.
        state.record_brief_delivery(
            publication_id="daily",
            brief_id=identity,
            source_id="ekot",
            published_at=delivered_at,
            delivered_at=delivered_at + timedelta(days=1),
            run_id="run",
        )
        assert state.delivered_brief_ids("daily") == frozenset({identity})
