"""Repairing Google Drive downloads that a Kobo device damaged on arrival."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from epub_news_feeder.kobo import (
    KoboRepairError,
    damaged_downloads,
    drive_download_root,
    repair_downloads,
)

_ERROR_BODY = (
    b'{\n  "error": {\n    "code": 401,\n    "message": "Request had invalid authentication '
    b'credentials.",\n    "status": "UNAUTHENTICATED"\n  }\n}'
)


def _device(tmp_path: Path) -> Path:
    root = tmp_path / "KOBOeReader"
    (root / ".kobo" / "google_drive" / "01_daily_news").mkdir(parents=True)
    return root


def _write(root: Path, name: str, body: bytes) -> Path:
    path = root / ".kobo" / "google_drive" / "01_daily_news" / name
    path.write_bytes(body)
    return path


def _epub(marker: bytes = b"payload") -> bytes:
    return b"PK\x03\x04" + marker


def test_drive_download_root_rejects_a_volume_that_is_not_a_kobo(tmp_path: Path) -> None:
    with pytest.raises(KoboRepairError, match="not a mounted Kobo"):
        drive_download_root(tmp_path / "Macintosh HD")


def test_intact_downloads_are_not_reported_as_damaged(tmp_path: Path) -> None:
    root = _device(tmp_path)
    _write(root, "good.epub", _epub())
    _write(root, "guide.pdf", b"%PDF-1.7 body")

    assert damaged_downloads(drive_download_root(root)) == ()


def test_a_prefixed_download_is_reported_with_its_payload(tmp_path: Path) -> None:
    root = _device(tmp_path)
    path = _write(root, "edition.epub", _ERROR_BODY + _epub())

    (damaged,) = damaged_downloads(drive_download_root(root))

    assert damaged.path == path
    assert damaged.prefix_bytes == len(_ERROR_BODY)
    assert damaged.payload_sha256 == sha256(_epub()).hexdigest()
    assert damaged.name == "edition.epub"


def test_a_download_with_no_recoverable_payload_is_left_alone(tmp_path: Path) -> None:
    root = _device(tmp_path)
    body = _ERROR_BODY + b" and nothing else"
    path = _write(root, "hopeless.epub", body)

    assert damaged_downloads(drive_download_root(root)) == ()
    assert path.read_bytes() == body


def test_repair_strips_the_prefix_once_drive_confirms_the_payload(tmp_path: Path) -> None:
    root = _device(tmp_path)
    path = _write(root, "edition.epub", _ERROR_BODY + _epub())
    backups = tmp_path / "backups"

    outcomes = repair_downloads(
        damaged_downloads(drive_download_root(root)),
        expected_sha256=lambda name: sha256(_epub()).hexdigest(),
        backup_directory=backups,
    )

    assert [outcome.repaired for outcome in outcomes] == [True]
    assert path.read_bytes() == _epub()
    assert (backups / "edition.epub").read_bytes() == _ERROR_BODY + _epub()


def test_repair_refuses_a_payload_drive_does_not_vouch_for(tmp_path: Path) -> None:
    root = _device(tmp_path)
    damaged_body = _ERROR_BODY + _epub()
    path = _write(root, "edition.epub", damaged_body)

    (outcome,) = repair_downloads(
        damaged_downloads(drive_download_root(root)),
        expected_sha256=lambda name: sha256(_epub(b"different")).hexdigest(),
        backup_directory=tmp_path / "backups",
    )

    assert outcome.repaired is False
    assert "does not match" in outcome.reason
    assert path.read_bytes() == damaged_body


def test_repair_refuses_a_download_drive_no_longer_holds(tmp_path: Path) -> None:
    root = _device(tmp_path)
    damaged_body = _ERROR_BODY + _epub()
    path = _write(root, "edition.epub", damaged_body)

    (outcome,) = repair_downloads(
        damaged_downloads(drive_download_root(root)),
        expected_sha256=lambda name: None,
        backup_directory=tmp_path / "backups",
    )

    assert outcome.repaired is False
    assert "not on Drive" in outcome.reason
    assert path.read_bytes() == damaged_body


def test_damaged_downloads_are_found_at_any_depth(tmp_path: Path) -> None:
    root = _device(tmp_path)
    archive = root / ".kobo" / "google_drive" / "01_daily_news" / "archive"
    archive.mkdir()
    (archive / "old.epub").write_bytes(_ERROR_BODY + _epub())

    names = [damaged.name for damaged in damaged_downloads(drive_download_root(root))]

    assert names == ["old.epub"]


def test_kobo_repair_reports_damage_without_writing_unless_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from epub_news_feeder.cli import main

    root = _device(tmp_path)
    damaged_body = _ERROR_BODY + _epub()
    path = _write(root, "edition.epub", damaged_body)

    code = main(["kobo-repair", "--volume", str(root)])

    assert code == 0
    assert path.read_bytes() == damaged_body
    output = capsys.readouterr().out
    assert "code=KOBO_DOWNLOAD_DAMAGED" in output
    assert "name=edition.epub" in output
    assert f"prefix_bytes={len(_ERROR_BODY)}" in output


def test_kobo_repair_reports_a_healthy_device(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from epub_news_feeder.cli import main

    root = _device(tmp_path)
    _write(root, "good.epub", _epub())

    assert main(["kobo-repair", "--volume", str(root)]) == 0
    assert "code=KOBO_DOWNLOADS_INTACT" in capsys.readouterr().out


def test_kobo_repair_rejects_a_volume_that_is_not_a_kobo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from epub_news_feeder.cli import main

    assert main(["kobo-repair", "--volume", str(tmp_path)]) == 2
    assert "code=KOBO_VOLUME_INVALID" in capsys.readouterr().err


def test_drive_digest_falls_back_to_the_archive_folder() -> None:
    from epub_news_feeder.cli import _drive_digest

    class _Client:
        def __init__(self) -> None:
            self.looked_in: list[str] = []

        def find_file(self, *, folder_id: str, filename: str) -> object | None:
            self.looked_in.append(folder_id)
            if folder_id != "archive":
                return None
            return SimpleNamespace(file_id="archived", sha256=None)

        def download(self, *, file_id: str) -> bytes:
            return _epub()

    client = _Client()
    digest = _drive_digest(cast(Any, client), ["delivery", "archive"])

    assert digest("edition.epub") == sha256(_epub()).hexdigest()
    assert client.looked_in == ["delivery", "archive"]


def test_drive_digest_declines_a_name_no_folder_holds() -> None:
    from epub_news_feeder.cli import _drive_digest

    class _Client:
        def find_file(self, *, folder_id: str, filename: str) -> object | None:
            return None

        def download(self, *, file_id: str) -> bytes:  # pragma: no cover - never reached
            raise AssertionError("a missing file must never be downloaded")

    assert _drive_digest(cast(Any, _Client()), ["delivery"])("gone.epub") is None
