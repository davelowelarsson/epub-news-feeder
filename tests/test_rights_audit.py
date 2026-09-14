"""The rights-audit report: expiry horizons for every Source's eligibility evidence.

Every Source expired on one calendar day (2026-09-08) and six mornings of Editions
silently failed. The audit makes the horizon visible on the same surfaces the operator
already reads — the CLI and the workflow job summary — without touching the fail-closed
gate itself: it reports, it never grants.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from epub_news_feeder.cli import main
from epub_news_feeder.config import load_config

_CONFIG = """
version: 1
sources:
  soonest:
    title: Soonest Source
    feed_url: https://soonest.example/feed.xml
    rights:
      basis: fixture_private_use
      audience: single_operator
      attribution_required: true
      media_reuse: false
    eligibility:
      evidence_reviewed_at: 2026-09-01
      review_expires_at: 2026-09-20
      evidence_id: fixture-soonest
      feed_acquisition: allow
  later:
    title: Later Source
    feed_url: https://later.example/feed.xml
    rights:
      basis: fixture_private_use
      audience: single_operator
      attribution_required: true
      media_reuse: false
    eligibility:
      evidence_reviewed_at: 2026-09-01
      review_expires_at: 2026-12-01
      evidence_id: fixture-later
      feed_acquisition: allow
  expired:
    title: Expired Source
    feed_url: https://expired.example/feed.xml
    rights:
      basis: fixture_private_use
      audience: single_operator
      attribution_required: true
      media_reuse: false
    eligibility:
      evidence_reviewed_at: 2026-08-01
      review_expires_at: 2026-09-08
      evidence_id: fixture-expired
      feed_acquisition: allow
  reviewless:
    title: Reviewless Source
    feed_url: https://reviewless.example/feed.xml
publications:
  - id: audit
    title: Audit Fixture
    policies:
      coverage:
        type: coverage
    sections:
      - id: all
        title: All
        policy: coverage
        sources: [soonest, later, expired, reviewless]
""".lstrip()


def _config_path(tmp_path: Path) -> Path:
    path = tmp_path / "audit.yaml"
    path.write_text(_CONFIG, encoding="utf-8")
    return path


def test_rights_audit_reports_horizons_soonest_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        ["rights-audit", "--config", str(_config_path(tmp_path)), "--at", "2026-09-14"]
    )

    assert exit_code == 0
    lines = capsys.readouterr().out.splitlines()
    order = [line.split()[0] for line in lines]
    # A Source with no review at all leads: it has never been eligible, which is more
    # urgent than a review that lapsed last week. Then soonest expiry, then source_id.
    assert order == [
        "source_id=reviewless",
        "source_id=expired",
        "source_id=soonest",
        "source_id=later",
    ]
    assert "expires=never-reviewed" in lines[0]
    assert "days_left=-6" in lines[1] and "expires=2026-09-08" in lines[1]
    assert "days_left=6" in lines[2]
    assert "days_left=78" in lines[3]


def test_rights_audit_markdown_marks_the_urgent_horizon(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "rights-audit",
            "--config",
            str(_config_path(tmp_path)),
            "--at",
            "2026-09-14",
            "--format",
            "markdown",
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].startswith("| Source |")
    assert any(line.startswith("| ⚠️ expired") for line in lines)
    assert any(line.startswith("| ⚠️ reviewless") for line in lines)
    assert any(line.startswith("| ⚠️ soonest") for line in lines)
    assert any(line.startswith("| later") for line in lines)


def test_rights_audit_within_filter_names_only_the_urgent_sources(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The workflow's escalation branch: `--within N` prints one source id per line for
    everything expired, unreviewed, or expiring within N days — and nothing else, so the
    step's emptiness test is the whole condition. Always exit 0: a report, never a gate."""

    exit_code = main(
        [
            "rights-audit",
            "--config",
            str(_config_path(tmp_path)),
            "--at",
            "2026-09-14",
            "--within",
            "7",
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out.split() == ["reviewless", "expired", "soonest"]

    # Well before any expiry, only the never-reviewed Source stays urgent — it has never
    # been eligible, so no date makes it quiet.
    exit_code = main(
        [
            "rights-audit",
            "--config",
            str(_config_path(tmp_path)),
            "--at",
            "2026-08-20",
            "--within",
            "7",
        ]
    )
    assert exit_code == 0
    assert capsys.readouterr().out.split() == ["reviewless"]


def test_rights_audit_rejects_an_invalid_config_like_validate_does(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("version: 2\nsources: {}\npublications: []\n", encoding="utf-8")

    exit_code = main(["rights-audit", "--config", str(path), "--at", "2026-09-14"])

    assert exit_code == 2


def test_reality_check_expiries_are_staggered_so_the_fleet_cannot_cliff_on_one_day() -> None:
    """Observed live: one shared review_expires_at took all 28 Sources down on the same
    morning. Renewals stay staggered — at least three distinct expiry dates — so a lapse
    degrades some Sections instead of silencing every Edition at once."""

    repository_root = Path(__file__).resolve().parents[1]
    configuration = load_config(repository_root / "examples" / "reality-check.yaml")
    expiries = {
        source.eligibility.review_expires_at
        for source in configuration.sources.values()
        if source.eligibility is not None
    }
    assert len(expiries) >= 3, "a single shared expiry recreates the 2026-09-09 outage"
