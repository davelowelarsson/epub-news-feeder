from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from epub_news_feeder.state import StateStore


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
