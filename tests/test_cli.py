from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


RUN_ID = re.compile(r"\b\d{8}T\d{6}Z-[A-Z2-7]{8}\b")


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["epub-news-feeder", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.acceptance
def test_ticket_01_installed_cli_reports_version() -> None:
    result = run_cli("--version")

    assert result.returncode == 0
    assert result.stdout.strip() == "epub-news-feeder 0.1.0"
    assert result.stderr == ""


@pytest.mark.acceptance
def test_ticket_01_invalid_generation_has_safe_run_id(tmp_path: Path) -> None:
    missing_config = tmp_path / "contains-secret-value-do-not-echo.yaml"

    result = run_cli(
        "generate",
        "--config",
        str(missing_config),
        "--state",
        str(tmp_path / "state.sqlite3"),
        "--output",
        str(tmp_path / "output"),
    )

    assert result.returncode == 2
    assert RUN_ID.search(result.stderr)
    assert "CONFIG_NOT_FOUND" in result.stderr
    assert "contains-secret-value-do-not-echo" not in result.stderr
    assert not (tmp_path / "state.sqlite3").exists()
    assert not (tmp_path / "output").exists()
