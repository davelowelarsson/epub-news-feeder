"""Repairing Google Drive downloads that a Kobo device damaged on arrival.

The device is removable, so an interrupted write is an ordinary event rather than an edge
case, and the tests below treat destroying a reader's book as the failure that matters most.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
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

_AT = datetime(2026, 9, 20, 18, 0, 0, tzinfo=UTC)

_ERROR_BODY = (
    b'{\n  "error": {\n    "code": 401,\n    "message": "Request had invalid authentication '
    b'credentials.",\n    "status": "UNAUTHENTICATED"\n  }\n}'
)


@pytest.fixture(autouse=True)
def _no_ambient_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    """The log folder defaults to an environment variable. On a machine where it is exported,
    a CLI test would upload fixture records to the real state folder."""

    monkeypatch.delenv("GOOGLE_DRIVE_FOLDER_DB", raising=False)


def _device(tmp_path: Path) -> Path:
    root = tmp_path / "KOBOeReader"
    (root / ".kobo" / "google_drive" / "01_daily_news").mkdir(parents=True)
    return root


def _write(root: Path, name: str, body: bytes, *, folder: str = "01_daily_news") -> Path:
    directory = root / ".kobo" / "google_drive" / folder
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(body)
    return path


def _epub(marker: bytes = b"payload") -> bytes:
    return b"PK\x03\x04" + marker


def _digests(*bodies: bytes) -> Any:
    known = tuple(sha256(body).hexdigest() for body in bodies)
    return lambda name: known


# --- detection -------------------------------------------------------------------------


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
    body = _ERROR_BODY + _epub()
    path = _write(root, "edition.epub", body)

    (damaged,) = damaged_downloads(drive_download_root(root))

    assert damaged.path == path
    assert damaged.prefix_bytes == len(_ERROR_BODY)
    assert damaged.payload_sha256 == sha256(_epub()).hexdigest()
    assert damaged.source_sha256 == sha256(body).hexdigest()
    assert damaged.name == "edition.epub"
    assert damaged.relative_path == Path("01_daily_news/edition.epub")


def test_a_download_with_no_recoverable_payload_is_left_alone(tmp_path: Path) -> None:
    root = _device(tmp_path)
    body = _ERROR_BODY + b" and nothing else"
    path = _write(root, "hopeless.epub", body)

    assert damaged_downloads(drive_download_root(root)) == ()
    assert path.read_bytes() == body


def test_symlinks_are_never_reported_or_followed(tmp_path: Path) -> None:
    root = _device(tmp_path)
    real = tmp_path / "outside.epub"
    real.write_bytes(_ERROR_BODY + _epub())
    link = root / ".kobo" / "google_drive" / "01_daily_news" / "link.epub"
    link.symlink_to(real)

    assert damaged_downloads(drive_download_root(root)) == ()


def test_damaged_downloads_are_found_at_any_depth(tmp_path: Path) -> None:
    root = _device(tmp_path)
    _write(root, "old.epub", _ERROR_BODY + _epub(), folder="01_daily_news/archive")

    (damaged,) = damaged_downloads(drive_download_root(root))

    assert damaged.relative_path == Path("01_daily_news/archive/old.epub")


# --- the repair itself -----------------------------------------------------------------


def test_repair_strips_the_prefix_once_drive_confirms_the_payload(tmp_path: Path) -> None:
    root = _device(tmp_path)
    path = _write(root, "edition.epub", _ERROR_BODY + _epub())
    backups = tmp_path / "backups"

    (outcome,) = repair_downloads(
        damaged_downloads(drive_download_root(root)),
        drive_digests=_digests(_epub()),
        backup_directory=backups,
    )

    assert outcome.repaired is True
    assert path.read_bytes() == _epub()
    assert (backups / "01_daily_news" / "edition.epub").read_bytes() == _ERROR_BODY + _epub()


def test_repair_refuses_a_payload_drive_does_not_vouch_for(tmp_path: Path) -> None:
    root = _device(tmp_path)
    damaged_body = _ERROR_BODY + _epub()
    path = _write(root, "edition.epub", damaged_body)

    (outcome,) = repair_downloads(
        damaged_downloads(drive_download_root(root)),
        drive_digests=_digests(_epub(b"different")),
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
        drive_digests=lambda name: (),
        backup_directory=tmp_path / "backups",
    )

    assert outcome.repaired is False
    assert "not on Drive" in outcome.reason
    assert path.read_bytes() == damaged_body


def test_a_name_held_twice_on_drive_is_repaired_against_the_matching_copy(
    tmp_path: Path,
) -> None:
    """Delivery and archive can hold the same name with different bytes; the device copy
    belongs to whichever one it matches, so a repair must not be refused by the other."""

    root = _device(tmp_path)
    archived = _epub(b"the archived edition")
    path = _write(root, "edition.epub", _ERROR_BODY + archived)

    (outcome,) = repair_downloads(
        damaged_downloads(drive_download_root(root)),
        drive_digests=_digests(_epub(b"a different edition"), archived),
        backup_directory=tmp_path / "backups",
    )

    assert outcome.repaired is True
    assert path.read_bytes() == archived


# --- finding 1: the verified bytes must be the written bytes ----------------------------


def test_a_file_changed_since_the_scan_is_never_rewritten(tmp_path: Path) -> None:
    """The digest is taken at scan time. If the bytes on disk change before the write, the
    stale digest must not authorise slicing the scan-time prefix off whatever is there now."""

    root = _device(tmp_path)
    path = _write(root, "edition.epub", _ERROR_BODY + _epub())
    damaged = damaged_downloads(drive_download_root(root))

    # Something else repairs it first — a second run of this very command would do this.
    path.write_bytes(_epub())

    (outcome,) = repair_downloads(
        damaged,
        drive_digests=_digests(_epub()),
        backup_directory=tmp_path / "backups",
    )

    assert outcome.repaired is False
    assert "changed" in outcome.reason
    assert path.read_bytes() == _epub()  # still a valid EPUB, header intact


# --- finding 2: an interrupted write must not destroy the book --------------------------


def test_an_interrupted_write_leaves_the_original_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _device(tmp_path)
    damaged_body = _ERROR_BODY + _epub()
    path = _write(root, "edition.epub", damaged_body)

    real_replace = os.replace

    def die(*args: object, **kwargs: object) -> None:
        raise OSError("device disconnected")

    monkeypatch.setattr(os, "replace", die)

    (outcome,) = repair_downloads(
        damaged_downloads(drive_download_root(root)),
        drive_digests=_digests(_epub()),
        backup_directory=tmp_path / "backups",
    )
    monkeypatch.setattr(os, "replace", real_replace)

    assert outcome.repaired is False
    assert path.read_bytes() == damaged_body  # untouched, not truncated
    assert list(path.parent.glob(".*repair*")) == []  # no debris left behind


def test_a_write_the_device_did_not_store_faithfully_is_reported_as_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """USB storage can accept a write and hold something else. Read it back before claiming
    the Edition was repaired; the backup is the reader's way out if it was not."""

    root = _device(tmp_path)
    _write(root, "edition.epub", _ERROR_BODY + _epub())
    real_replace = os.replace

    def replace_then_rot(source: object, target: object) -> None:
        real_replace(cast(Any, source), cast(Any, target))
        Path(cast(Any, target)).write_bytes(b"PK\x03\x04 something else entirely")

    monkeypatch.setattr(os, "replace", replace_then_rot)
    (outcome,) = repair_downloads(
        damaged_downloads(drive_download_root(root)),
        drive_digests=_digests(_epub()),
        backup_directory=tmp_path / "backups",
    )
    monkeypatch.setattr(os, "replace", real_replace)

    assert outcome.repaired is False
    assert "does not match what was sent" in outcome.reason
    assert (tmp_path / "backups" / "01_daily_news" / "edition.epub").exists()


# --- finding 3: backups must never overwrite each other ---------------------------------


def test_same_named_downloads_in_different_folders_get_separate_backups(
    tmp_path: Path,
) -> None:
    root = _device(tmp_path)
    current, archived = _epub(b"current"), _epub(b"archived")
    _write(root, "edition.epub", _ERROR_BODY + current)
    _write(root, "edition.epub", _ERROR_BODY + archived, folder="01_daily_news/archive")
    backups = tmp_path / "backups"

    outcomes = repair_downloads(
        damaged_downloads(drive_download_root(root)),
        drive_digests=_digests(current, archived),
        backup_directory=backups,
    )

    assert [o.repaired for o in outcomes] == [True, True]
    assert (backups / "01_daily_news" / "edition.epub").read_bytes() == _ERROR_BODY + current
    assert (
        backups / "01_daily_news" / "archive" / "edition.epub"
    ).read_bytes() == _ERROR_BODY + archived


def test_a_second_run_never_overwrites_an_earlier_backup(tmp_path: Path) -> None:
    root = _device(tmp_path)
    first = _ERROR_BODY + _epub(b"first")
    path = _write(root, "edition.epub", first)
    backups = tmp_path / "backups"

    repair_downloads(
        damaged_downloads(drive_download_root(root)),
        drive_digests=_digests(_epub(b"first")),
        backup_directory=backups,
    )
    # The device damages it again, and the tool runs a second time.
    second = _ERROR_BODY + _epub(b"second")
    path.write_bytes(second)
    repair_downloads(
        damaged_downloads(drive_download_root(root)),
        drive_digests=_digests(_epub(b"second")),
        backup_directory=backups,
    )

    kept = sorted(p.read_bytes() for p in (backups / "01_daily_news").iterdir())
    assert kept == sorted([first, second])


# --- finding 4: a failure must not hide repairs already made ----------------------------


def test_a_drive_failure_still_reports_what_was_already_repaired(tmp_path: Path) -> None:
    root = _device(tmp_path)
    good = _epub(b"first")
    first = _write(root, "a-edition.epub", _ERROR_BODY + good)
    _write(root, "b-edition.epub", _ERROR_BODY + _epub(b"second"))

    def digests(name: str) -> tuple[str, ...]:
        if name.startswith("b-"):
            raise RuntimeError("Drive lookup failed")
        return (sha256(good).hexdigest(),)

    outcomes = repair_downloads(
        damaged_downloads(drive_download_root(root)),
        drive_digests=digests,
        backup_directory=tmp_path / "backups",
    )

    assert [(o.name, o.repaired) for o in outcomes] == [
        ("a-edition.epub", True),
        ("b-edition.epub", False),
    ]
    assert "Drive lookup failed" in outcomes[1].reason
    assert first.read_bytes() == good


# --- CLI --------------------------------------------------------------------------------


def test_kobo_repair_reports_damage_without_writing_unless_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from epub_news_feeder.cli import main

    root = _device(tmp_path)
    damaged_body = _ERROR_BODY + _epub()
    path = _write(root, "edition.epub", damaged_body)

    code = main(["kobo-repair", "--volume", str(root), "--log-dir", str(tmp_path / "scans")])

    assert code == 0
    assert path.read_bytes() == damaged_body
    output = capsys.readouterr().out
    assert "code=KOBO_DOWNLOAD_DAMAGED" in output
    assert "name=edition.epub" in output
    assert f"prefix_bytes={len(_ERROR_BODY)}" in output


def test_kobo_repair_applies_the_repair_against_drive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import epub_news_feeder.cli as cli

    root = _device(tmp_path)
    path = _write(root, "edition.epub", _ERROR_BODY + _epub())
    monkeypatch.setattr(cli, "credentials_from_environment", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "HttpDriveClient", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(cli, "_drive_digests", lambda client, folders: _digests(_epub()))

    code = cli.main(
        [
            "kobo-repair",
            "--volume",
            str(root),
            "--apply",
            "--drive-folder",
            "delivery",
            "--backup",
            str(tmp_path / "backups"),
            "--log-dir",
            str(tmp_path / "scans"),
        ]
    )

    assert code == 0
    assert path.read_bytes() == _epub()
    assert "code=KOBO_DOWNLOAD_REPAIRED" in capsys.readouterr().out


def test_kobo_repair_reports_a_healthy_device(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from epub_news_feeder.cli import main

    root = _device(tmp_path)
    _write(root, "good.epub", _epub())

    arguments = ["kobo-repair", "--volume", str(root), "--log-dir", str(tmp_path / "scans")]
    assert main(arguments) == 0
    assert "code=KOBO_DOWNLOADS_INTACT" in capsys.readouterr().out


def test_kobo_repair_rejects_a_volume_that_is_not_a_kobo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from epub_news_feeder.cli import main

    assert main(["kobo-repair", "--volume", str(tmp_path)]) == 2
    assert "code=KOBO_VOLUME_INVALID" in capsys.readouterr().err


def test_drive_digests_collects_every_folder_holding_the_name() -> None:
    from epub_news_feeder.cli import _drive_digests

    bodies = {"delivery": _epub(b"current"), "archive": _epub(b"archived")}

    class _Client:
        def __init__(self) -> None:
            self.looked_in: list[str] = []

        def find_file(self, *, folder_id: str, filename: str) -> object | None:
            self.looked_in.append(folder_id)
            return SimpleNamespace(file_id=folder_id, sha256=None)

        def download(self, *, file_id: str) -> bytes:
            return bodies[file_id]

    client = _Client()
    got = _drive_digests(cast(Any, client), ["delivery", "archive"])("edition.epub")

    assert client.looked_in == ["delivery", "archive"]
    assert set(got) == {sha256(b).hexdigest() for b in bodies.values()}


def test_drive_digests_declines_a_name_no_folder_holds() -> None:
    from epub_news_feeder.cli import _drive_digests

    class _Client:
        def find_file(self, *, folder_id: str, filename: str) -> object | None:
            return None

        def download(self, *, file_id: str) -> bytes:  # pragma: no cover - never reached
            raise AssertionError("a missing file must never be downloaded")

    assert _drive_digests(cast(Any, _Client()), ["delivery"])("gone.epub") == ()


# --- second review round -----------------------------------------------------------------


def test_a_replaced_file_that_cannot_be_read_back_is_reported_as_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the rename lands the device has changed. Saying "left untouched" because the
    read-back failed would send the reader looking in the wrong place."""

    root = _device(tmp_path)
    path = _write(root, "edition.epub", _ERROR_BODY + _epub())
    damaged = damaged_downloads(drive_download_root(root))
    real_read = Path.read_bytes
    calls = {"n": 0}

    def flaky(self: Path) -> bytes:
        calls["n"] += 1
        if calls["n"] > 2 and self == path:
            raise OSError("device disconnected")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", flaky)
    (outcome,) = repair_downloads(
        damaged, drive_digests=_digests(_epub()), backup_directory=tmp_path / "backups"
    )
    monkeypatch.setattr(Path, "read_bytes", real_read)

    assert outcome.repaired is False
    assert outcome.uncertain is True
    assert "read back" in outcome.reason
    assert path.read_bytes() == _epub()  # it really was replaced


def test_a_mismatched_read_back_is_uncertain_not_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _device(tmp_path)
    _write(root, "edition.epub", _ERROR_BODY + _epub())
    real_replace = os.replace

    def replace_then_rot(source: object, target: object) -> None:
        real_replace(cast(Any, source), cast(Any, target))
        Path(cast(Any, target)).write_bytes(b"PK\x03\x04 something else")

    monkeypatch.setattr(os, "replace", replace_then_rot)
    (outcome,) = repair_downloads(
        damaged_downloads(drive_download_root(root)),
        drive_digests=_digests(_epub()),
        backup_directory=tmp_path / "backups",
    )
    monkeypatch.setattr(os, "replace", real_replace)

    assert outcome.repaired is False
    assert outcome.uncertain is True


def test_a_source_changed_during_the_drive_lookup_is_not_overwritten(tmp_path: Path) -> None:
    """The Drive lookup is a network round trip. A file that changed during it must not be
    replaced by a payload verified against what was there beforehand."""

    root = _device(tmp_path)
    path = _write(root, "edition.epub", _ERROR_BODY + _epub())
    damaged = damaged_downloads(drive_download_root(root))

    def digests_then_meddle(name: str) -> tuple[str, ...]:
        path.write_bytes(_epub(b"someone else got here first"))
        return (sha256(_epub()).hexdigest(),)

    (outcome,) = repair_downloads(
        damaged, drive_digests=digests_then_meddle, backup_directory=tmp_path / "backups"
    )

    assert outcome.repaired is False
    assert "changed" in outcome.reason
    assert path.read_bytes() == _epub(b"someone else got here first")


def test_the_temporary_file_is_unique_per_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fixed temp name lets one run truncate another's half-written replacement."""

    root = _device(tmp_path)
    path = _write(root, "edition.epub", _ERROR_BODY + _epub())
    seen: list[str] = []
    real_replace = os.replace

    def record(source: Any, target: Any, *, src_dir_fd: Any = None, dst_dir_fd: Any = None) -> None:
        seen.append(Path(source).name)
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", record)
    for _ in range(2):
        path.write_bytes(_ERROR_BODY + _epub())
        repair_downloads(
            damaged_downloads(drive_download_root(root)),
            drive_digests=_digests(_epub()),
            backup_directory=tmp_path / "backups",
        )
    monkeypatch.setattr(os, "replace", real_replace)

    assert len(seen) == 2
    assert seen[0] != seen[1]
    assert list(path.parent.glob(".*repair*")) == []


def test_healthy_downloads_are_not_read_in_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scanning must not pull every healthy book into memory to decide it is healthy."""

    root = _device(tmp_path)
    _write(root, "good.epub", _epub(b"x" * 100_000))

    def refuse(self: Path) -> bytes:
        raise AssertionError(f"{self.name} was read in full during detection")

    monkeypatch.setattr(Path, "read_bytes", refuse)

    assert damaged_downloads(drive_download_root(root)) == ()


def test_drive_digests_ignores_a_folder_it_cannot_reach(tmp_path: Path) -> None:
    from epub_news_feeder.cli import _drive_digests

    class _Client:
        def find_file(self, *, folder_id: str, filename: str) -> object | None:
            if folder_id == "broken":
                raise RuntimeError("Drive folder unreachable")
            return SimpleNamespace(file_id="ok", sha256=None)

        def download(self, *, file_id: str) -> bytes:
            return _epub()

    got = _drive_digests(cast(Any, _Client()), ["broken", "delivery"])("edition.epub")

    assert got == (sha256(_epub()).hexdigest(),)


def test_drive_digests_raises_when_every_folder_failed() -> None:
    """No answer is not the same as "Drive does not have it"; refusing on an outage would
    read as a mismatch and hide a real problem."""

    from epub_news_feeder.cli import _drive_digests

    class _Client:
        def find_file(self, *, folder_id: str, filename: str) -> object | None:
            raise RuntimeError("Drive unreachable")

        def download(self, *, file_id: str) -> bytes:  # pragma: no cover
            raise AssertionError("never reached")

    with pytest.raises(RuntimeError, match="Drive unreachable"):
        _drive_digests(cast(Any, _Client()), ["a", "b"])("edition.epub")


# --- scan log ----------------------------------------------------------------------------


def test_the_firmware_version_is_read_without_the_serial_number(tmp_path: Path) -> None:
    """The version file leads with the device serial, which has no place in a shared log."""

    from epub_news_feeder.kobo import device_firmware

    root = _device(tmp_path)
    (root / ".kobo" / "version").write_text(
        "N428631101026,4.9.77,5.18.270971,4.9.77,4.9.77,00000000-0000-0000-0000-000000000390"
    )

    assert device_firmware(root) == "5.18.270971"


def test_firmware_is_absent_rather_than_guessed_when_unreadable(tmp_path: Path) -> None:
    from epub_news_feeder.kobo import device_firmware

    assert device_firmware(_device(tmp_path)) is None


def test_a_scan_record_carries_the_counts_and_no_serial(tmp_path: Path) -> None:
    from epub_news_feeder.kobo import scan_record

    root = _device(tmp_path)
    (root / ".kobo" / "version").write_text("SERIAL123,4.9.77,5.18.270971,4.9.77")
    _write(root, "good.epub", _epub())
    _write(root, "edition.epub", _ERROR_BODY + _epub())
    damaged = damaged_downloads(drive_download_root(root))

    record = scan_record(volume=root, damaged=damaged, outcomes=(), at=_AT)

    assert record["at"] == "2026-09-20T18:00:00Z"
    assert record["firmware"] == "5.18.270971"
    assert record["counts"] == {
        "downloads": 2,
        "damaged": 1,
        "repaired": 0,
        "uncertain": 0,
        "unchanged": 0,
    }
    assert record["damaged"] == [
        {"name": "edition.epub", "folder": "01_daily_news", "prefix_bytes": len(_ERROR_BODY)}
    ]
    assert "SERIAL123" not in json.dumps(record)


def test_a_scan_record_summarises_what_a_repair_did(tmp_path: Path) -> None:
    from epub_news_feeder.kobo import scan_record

    root = _device(tmp_path)
    _write(root, "edition.epub", _ERROR_BODY + _epub())
    damaged = damaged_downloads(drive_download_root(root))
    outcomes = repair_downloads(
        damaged, drive_digests=_digests(_epub()), backup_directory=tmp_path / "b"
    )

    record = scan_record(volume=root, damaged=damaged, outcomes=outcomes, at=_AT)

    assert record["counts"]["repaired"] == 1
    assert record["outcomes"] == [
        {
            "name": "edition.epub",
            "folder": "01_daily_news",
            "status": "repaired",
            "reason": f"stripped {len(_ERROR_BODY)} bytes",
        }
    ]


def test_scans_append_to_one_file_per_month(tmp_path: Path) -> None:
    from epub_news_feeder.kobo import append_scan, scan_record

    root = _device(tmp_path)
    logs = tmp_path / "scans"
    first = scan_record(volume=root, damaged=(), outcomes=(), at=_AT)
    second = scan_record(volume=root, damaged=(), outcomes=(), at=_AT.replace(day=28))
    october = scan_record(volume=root, damaged=(), outcomes=(), at=_AT.replace(month=10))

    paths = [append_scan(r, log_directory=logs) for r in (first, second, october)]

    assert paths[0] == paths[1] == logs / "kobo-scans-2026-09.jsonl"
    assert paths[2] == logs / "kobo-scans-2026-10.jsonl"
    lines = paths[0].read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["at"] == "2026-09-28T18:00:00Z"
    assert paths[2].read_text().splitlines() == [json.dumps(october, sort_keys=True)]


def test_kobo_repair_logs_every_scan_locally(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from epub_news_feeder.cli import main

    root = _device(tmp_path)
    _write(root, "edition.epub", _ERROR_BODY + _epub())
    logs = tmp_path / "scans"

    assert main(["kobo-repair", "--volume", str(root), "--log-dir", str(logs)]) == 0

    (written,) = list(logs.glob("*.jsonl"))
    record = json.loads(written.read_text().splitlines()[0])
    assert record["counts"]["damaged"] == 1
    assert record["outcomes"] == []
    assert f"code=KOBO_SCAN_LOGGED path={written}" in capsys.readouterr().out


def test_a_scan_log_is_uploaded_to_the_drive_state_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import epub_news_feeder.cli as cli

    root = _device(tmp_path)
    _write(root, "good.epub", _epub())
    uploaded: dict[str, Any] = {}

    class _Client:
        def find_file(self, *, folder_id: str, filename: str) -> object | None:
            return None

        def upload(
            self, *, folder_id: str, filename: str, content: bytes, content_type: str
        ) -> str:
            uploaded.update(folder=folder_id, name=filename, body=content, kind=content_type)
            return "new-id"

    monkeypatch.setattr(cli, "credentials_from_environment", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "HttpDriveClient", lambda **kwargs: _Client())

    code = cli.main(
        [
            "kobo-repair",
            "--volume",
            str(root),
            "--log-dir",
            str(tmp_path / "scans"),
            "--log-folder",
            "state-folder",
        ]
    )

    assert code == 0
    assert uploaded["folder"] == "state-folder"
    assert uploaded["name"].startswith("kobo-scans-")
    assert uploaded["kind"] == "application/x-ndjson"
    assert json.loads(uploaded["body"].decode().splitlines()[0])["counts"]["downloads"] == 1
    assert "code=KOBO_SCAN_UPLOADED" in capsys.readouterr().out


def test_a_failed_upload_never_fails_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The scan already happened and the local record already exists; losing the copy to Drive
    is worth a word on stderr, not a non-zero exit."""

    import epub_news_feeder.cli as cli

    root = _device(tmp_path)
    _write(root, "good.epub", _epub())
    monkeypatch.setattr(cli, "credentials_from_environment", lambda: SimpleNamespace())

    def explode(**kwargs: object) -> object:
        raise RuntimeError("Drive unreachable")

    monkeypatch.setattr(cli, "HttpDriveClient", explode)

    code = cli.main(
        [
            "kobo-repair",
            "--volume",
            str(root),
            "--log-dir",
            str(tmp_path / "scans"),
            "--log-folder",
            "state-folder",
        ]
    )

    assert code == 0
    assert list((tmp_path / "scans").glob("*.jsonl"))
    assert "code=KOBO_SCAN_UPLOAD_FAILED" in capsys.readouterr().err


def test_logging_can_be_turned_off(tmp_path: Path) -> None:
    from epub_news_feeder.cli import main

    root = _device(tmp_path)
    _write(root, "good.epub", _epub())
    logs = tmp_path / "scans"

    assert main(["kobo-repair", "--volume", str(root), "--log-dir", str(logs), "--no-log"]) == 0
    assert not logs.exists()


def test_the_suite_never_writes_to_the_real_scan_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The log directory defaults to a path inside the repository, so a CLI test that forgets
    to redirect it appends fixture data to a record meant to measure a real device."""

    from epub_news_feeder.cli import _parser, main

    default = Path(_parser().parse_args(["kobo-repair", "--volume", "/x"]).log_dir)
    before = sorted(default.glob("*.jsonl")) if default.exists() else []

    root = _device(tmp_path)
    _write(root, "good.epub", _epub())
    monkeypatch.delenv("GOOGLE_DRIVE_FOLDER_DB", raising=False)
    main(["kobo-repair", "--volume", str(root), "--log-dir", str(tmp_path / "scans")])

    after = sorted(default.glob("*.jsonl")) if default.exists() else []
    assert after == before
    assert (tmp_path / "scans").exists()


def test_a_scan_that_cannot_be_uploaded_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silence here reads as success. Without a Drive folder the record is local only, which
    is worth one line rather than an absent one the reader has to notice."""

    from epub_news_feeder.cli import main

    root = _device(tmp_path)
    _write(root, "good.epub", _epub())
    monkeypatch.delenv("GOOGLE_DRIVE_FOLDER_DB", raising=False)

    assert main(["kobo-repair", "--volume", str(root), "--log-dir", str(tmp_path / "s")]) == 0

    output = capsys.readouterr().out
    assert "code=KOBO_SCAN_NOT_UPLOADED" in output
    assert "--env-file" in output


@pytest.mark.parametrize(
    "content",
    [
        "SERIAL_A,4.9.77,\nSERIAL_B,x,y",  # a second line's serial landed in field 2
        "SERIAL_A,4.9.77,not a version",
        "SERIAL_A,4.9.77,../../etc/passwd",
        "SERIAL_A",
        "",
    ],
)
def test_firmware_is_none_rather_than_whatever_was_in_that_field(
    tmp_path: Path, content: str
) -> None:
    """The record is meant to be publishable. A field that is not a firmware version is not
    reported as one — the serial sits one field away."""

    from epub_news_feeder.kobo import device_firmware

    root = _device(tmp_path)
    (root / ".kobo" / "version").write_text(content)

    assert device_firmware(root) is None


def test_firmware_reads_only_the_first_line(tmp_path: Path) -> None:
    from epub_news_feeder.kobo import device_firmware

    root = _device(tmp_path)
    (root / ".kobo" / "version").write_text("N4286,4.9.77,5.18.270971,4.9.77\nSERIAL_B,1,2\n")

    assert device_firmware(root) == "5.18.270971"


def test_outcomes_for_the_same_name_in_two_folders_stay_distinct(tmp_path: Path) -> None:
    """The delivery folder and the archive can hold one name, and on the real device they do.
    Collapsing them loses a repair and labels it as if nothing happened."""

    from epub_news_feeder.kobo import scan_record

    root = _device(tmp_path)
    current, archived = _epub(b"current"), _epub(b"archived")
    _write(root, "same.epub", _ERROR_BODY + current)
    _write(root, "same.epub", _ERROR_BODY + archived, folder="01_daily_news/archive")
    damaged = damaged_downloads(drive_download_root(root))
    outcomes = repair_downloads(
        damaged,
        drive_digests=lambda name: (sha256(current).hexdigest(),),  # only the delivery copy
        backup_directory=tmp_path / "b",
    )

    record = scan_record(volume=root, damaged=damaged, outcomes=outcomes, at=_AT)

    assert record["counts"]["repaired"] == 1
    assert record["counts"]["unchanged"] == 1
    # Scanning sorts by path, so the archive copy is reported first.
    assert [(o["folder"], o["status"]) for o in record["outcomes"]] == [
        ("01_daily_news/archive", "unchanged"),
        ("01_daily_news", "repaired"),
    ]


def test_an_unreadable_version_file_does_not_break_the_scan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Logging is bookkeeping. It must not change what the command reports about the device."""

    from epub_news_feeder.cli import main

    root = _device(tmp_path)
    _write(root, "good.epub", _epub())
    (root / ".kobo" / "version").write_bytes(b"\xff\xfe not utf-8 at all")
    monkeypatch.delenv("GOOGLE_DRIVE_FOLDER_DB", raising=False)

    assert main(["kobo-repair", "--volume", str(root), "--log-dir", str(tmp_path / "s")]) == 0
    assert "code=KOBO_DOWNLOADS_INTACT" in capsys.readouterr().out
