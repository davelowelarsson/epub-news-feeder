"""The Source health digest: a read-only accessor plus the ``source-health`` CLI report.

Nine of 28 Sources failed on one ordinary morning and nothing surfaced it - the reader-facing
note fires every day regardless, so an operator watching only the Edition never sees which
Source is actually broken. This report closes that gap without opening a second writer: it
reads the State Store directly, worst Source first, and never touches the lock or key
sidecar files the writer path depends on.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from epub_news_feeder.cli import main
from epub_news_feeder.state import SourceHealth, StateStore, read_source_health


def _seed(state_path: Path) -> None:
    with StateStore(state_path, environment="test") as state:
        # Healthy: one clean attempt, no failures, a real last_success date.
        state.record_source_health(
            "healthy",
            attempted_at=datetime(2026, 8, 20, 6, tzinfo=UTC),
            succeeded=True,
            classification="SOURCE_OK",
        )
        # Two Sources tied at exactly the warning threshold, to prove the source_id tiebreak.
        for source_id in ("beta", "alpha"):
            for _ in range(3):
                state.record_source_health(
                    source_id,
                    attempted_at=datetime(2026, 8, 21, 6, tzinfo=UTC),
                    succeeded=False,
                    classification="SOURCE_FETCH_FAILED",
                )
        # Never succeeded at all, and the worst failure count present.
        for _ in range(5):
            state.record_source_health(
                "zeta",
                attempted_at=datetime(2026, 8, 21, 6, tzinfo=UTC),
                succeeded=False,
                classification="SOURCE_UNREACHABLE",
            )


# --- read_source_health -------------------------------------------------------------


def test_read_source_health_returns_every_recorded_source(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite3"
    _seed(state_path)

    records = read_source_health(state_path)

    assert {record.source_id for record in records} == {"healthy", "beta", "alpha", "zeta"}
    zeta = next(record for record in records if record.source_id == "zeta")
    assert zeta.consecutive_failures == 5
    assert zeta.last_success is None
    assert zeta.response_classification == "SOURCE_UNREACHABLE"
    healthy = next(record for record in records if record.source_id == "healthy")
    assert healthy.consecutive_failures == 0
    assert healthy.last_success == datetime(2026, 8, 20, 6, tzinfo=UTC)


def test_read_source_health_returns_empty_for_a_missing_database(tmp_path: Path) -> None:
    assert read_source_health(tmp_path / "does-not-exist.sqlite3") == []


def test_read_source_health_does_not_take_the_writer_lock_or_touch_sidecar_files(
    tmp_path: Path,
) -> None:
    """No ``.key`` or ``.lock`` file may appear: those exist only for the writer path."""

    state_path = tmp_path / "state.sqlite3"
    _seed(state_path)
    # Prove independence from the sidecar files entirely: remove them and read anyway.
    state_path.with_suffix(".sqlite3.key").unlink()
    state_path.with_suffix(".sqlite3.lock").unlink()

    records = read_source_health(state_path)

    assert len(records) == 4
    assert not state_path.with_suffix(".sqlite3.key").exists()
    assert not state_path.with_suffix(".sqlite3.lock").exists()


def test_read_source_health_is_a_frozen_value_type(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite3"
    _seed(state_path)

    (record,) = [
        record for record in read_source_health(state_path) if record.source_id == "healthy"
    ]

    assert isinstance(record, SourceHealth)


# --- source-health CLI ----------------------------------------------------------------


def test_source_health_cli_reports_nothing_for_a_missing_state_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["source-health", "--state", str(tmp_path / "absent.sqlite3")])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out.strip().count("\n") == 0
    assert captured.out.strip() != ""


def test_source_health_cli_text_format_is_sorted_worst_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_path = tmp_path / "state.sqlite3"
    _seed(state_path)

    exit_code = main(["source-health", "--state", str(state_path)])

    assert exit_code == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    source_order = [line.split()[0] for line in lines]
    assert source_order == [
        "source_id=zeta",
        "source_id=alpha",
        "source_id=beta",
        "source_id=healthy",
    ]
    assert "consecutive_failures=5" in lines[0]
    assert "last_success=never" in lines[0]
    assert "last_success=2026-08-20" in lines[3]


def test_source_health_cli_markdown_format_marks_repeated_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_path = tmp_path / "state.sqlite3"
    _seed(state_path)

    exit_code = main(["source-health", "--state", str(state_path), "--format", "markdown"])

    assert exit_code == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[0].startswith("|")
    assert "---" in lines[1]
    assert any(line.startswith("| ⚠️ zeta") for line in lines)
    assert any(line.startswith("| ⚠️ alpha") for line in lines)
    assert any(line.startswith("| healthy") for line in lines)
    # Never a title or URL, only ids and codes: this feeds a world-readable log.
    assert "SOURCE_FETCH_FAILED" in captured.out
    assert "SOURCE_UNREACHABLE" in captured.out


def test_source_health_cli_never_takes_the_writer_lock(tmp_path: Path) -> None:
    """The report must run cleanly even with the writer's sidecar files gone entirely.

    Deletes the fingerprint key and the writer's advisory lock file before reporting: SQLite
    itself may still create its own ``-wal``/``-shm`` files while reading a WAL-mode database,
    but those are SQLite's read machinery, not this project's writer-lock/key sidecars, and a
    read-only report has no reason to ever create either of ours.
    """

    state_path = tmp_path / "state.sqlite3"
    _seed(state_path)
    state_path.with_suffix(".sqlite3.key").unlink()
    state_path.with_suffix(".sqlite3.lock").unlink()

    exit_code = main(["source-health", "--state", str(state_path)])

    assert exit_code == 0
    after = set(os.listdir(tmp_path))
    assert "state.sqlite3.key" not in after
    assert "state.sqlite3.lock" not in after
