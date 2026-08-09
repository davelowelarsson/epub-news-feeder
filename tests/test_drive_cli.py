from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from epub_news_feeder.cli import main


@pytest.mark.contract
def test_authorize_drive_help_is_available() -> None:
    result = subprocess.run(
        ["epub-news-feeder", "authorize-drive", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--client-secret" in result.stdout
    assert "drive.file" in result.stdout


def test_authorize_drive_reports_a_clear_error_when_no_client_secret_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = main(["authorize-drive"])

    assert exit_code == 3
    captured = capsys.readouterr()
    assert "code=DRIVE_AUTHORIZATION_FAILED" in captured.err
    assert "No client_secret" in captured.err


@pytest.mark.security
def test_generate_reports_missing_drive_credentials_without_leaking_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """version: 1
sources:
  source:
    title: Source
    feed_url: https://example.com/feed.xml
publications:
  - id: daily
    title: Daily
    sections:
      - id: news
        title: News
        sources: [source]
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_REFRESH_TOKEN", raising=False)

    exit_code = main(
        [
            "generate",
            "--config",
            str(config),
            "--state",
            str(tmp_path / "state.sqlite3"),
            "--output",
            str(tmp_path / "output"),
            "--drive-folder",
            "folder-1",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "code=DRIVE_CONFIGURATION_INVALID" in captured.err
    assert "GOOGLE_OAUTH_CLIENT_ID" in captured.err
