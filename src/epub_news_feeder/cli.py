from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import webbrowser
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from epub_news_feeder import __version__
from epub_news_feeder.application import (
    DriveTarget,
    GenerationError,
    StateSyncTarget,
    generate_edition,
)
from epub_news_feeder.config import ConfigError, load_config
from epub_news_feeder.drive import (
    DriveConfigurationError,
    HttpDriveClient,
    credentials_from_environment,
)
from epub_news_feeder.drive_oauth import (
    DriveAuthorizationError,
    authorize,
    find_client_secret,
    load_client_secret,
)
from epub_news_feeder.kobo import (
    KoboRepairError,
    append_scan,
    damaged_downloads,
    drive_download_root,
    repair_downloads,
    scan_record,
)
from epub_news_feeder.ollama import OllamaError, check_ollama
from epub_news_feeder.run_id import create_run_id
from epub_news_feeder.state import SourceHealth, read_source_health
from epub_news_feeder.state_sync import (
    StateSyncAuthError,
    StateSyncError,
    restore_state,
    save_state,
)

_RUN_ID = re.compile(r"^\d{8}T\d{6}Z-[A-Z2-7]{8}$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="epub-news-feeder",
        description="Build private, finite news Editions as standards-first EPUBs.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate", help="Generate and locally deliver an Edition.")
    generate.add_argument("--config", required=True, type=Path)
    generate.add_argument("--state", required=True, type=Path)
    generate.add_argument("--output", required=True, type=Path)
    generate.add_argument("--diagnostics", type=Path)
    generate.add_argument("--publication")
    generate.add_argument("--run-id")
    generate.add_argument("--at")
    generate.add_argument("--epubcheck-jar", type=Path)
    generate.add_argument(
        "--drive-folder", help="Also deliver to this Google Drive folder ID (opt-in)."
    )
    generate.add_argument(
        "--archive-folder",
        help=(
            "Move Editions past retention from the delivery folder to this Drive folder ID "
            "after a successful delivery (opt-in); defaults to GOOGLE_DRIVE_FOLDER_ARCHIVE."
        ),
    )
    generate.add_argument(
        "--state-folder",
        help=(
            "Restore/save the State Store from/to this Google Drive folder ID (opt-in); "
            "defaults to GOOGLE_DRIVE_FOLDER_DB."
        ),
    )
    generate.add_argument(
        "--state-environment",
        default="local",
        help="Scheduled State Store archive name suffix (state-<environment>.tar.gz).",
    )

    validate = commands.add_parser("validate", help="Validate configuration without side effects.")
    validate.add_argument("--config", required=True, type=Path)

    ollama = commands.add_parser(
        "ollama-check", help="Verify a local Ollama model and strict JSON output."
    )
    ollama.add_argument("--host", default="http://127.0.0.1:11434")
    ollama.add_argument("--model", required=True)

    authorize_drive = commands.add_parser(
        "authorize-drive",
        help="One-time interactive Google Drive authorization (drive.file scope).",
        description="One-time interactive Google Drive authorization (drive.file scope).",
    )
    authorize_drive.add_argument(
        "--client-secret",
        type=Path,
        help="Path to the downloaded client_secret_*.json; defaults to the one in the cwd.",
    )

    state_pull = commands.add_parser(
        "state-pull", help="Debug: restore the State Store from Drive, verifying its digest."
    )
    state_pull.add_argument("--state", required=True, type=Path)
    state_pull.add_argument("--state-folder", required=True)
    state_pull.add_argument("--state-environment", default="local")

    state_push = commands.add_parser(
        "state-push", help="Debug: save the State Store to Drive, overwriting it in place."
    )
    state_push.add_argument("--state", required=True, type=Path)
    state_push.add_argument("--state-folder", required=True)
    state_push.add_argument("--state-environment", default="local")

    source_health = commands.add_parser(
        "source-health",
        help="Report per-Source health from the State Store (read-only; never a gate).",
    )
    source_health.add_argument("--state", required=True, type=Path)
    source_health.add_argument("--format", choices=["text", "markdown"], default="text")

    rights_audit = commands.add_parser(
        "rights-audit",
        help="Report every Source's rights-review expiry horizon (a report, never a gate).",
    )
    rights_audit.add_argument("--config", required=True, type=Path)
    rights_audit.add_argument("--format", choices=["text", "markdown"], default="text")
    rights_audit.add_argument(
        "--at", help="Audit relative to this ISO date instead of today (for reproducibility)."
    )
    kobo_repair = commands.add_parser(
        "kobo-repair",
        help=(
            "Repair Google Drive downloads a Kobo damaged on arrival (reports only unless "
            "--apply is given)."
        ),
    )
    kobo_repair.add_argument(
        "--volume", type=Path, default=Path("/Volumes/KOBOeReader"), help="The mounted Kobo."
    )
    kobo_repair.add_argument(
        "--apply",
        action="store_true",
        help="Strip the error body from every download Drive vouches for, byte for byte.",
    )
    kobo_repair.add_argument(
        "--drive-folder",
        action="append",
        default=None,
        help=(
            "A Drive folder whose files are the repair's source of truth; repeat it to cover "
            "the archive as well as the delivery folder."
        ),
    )
    kobo_repair.add_argument(
        "--log-dir",
        type=Path,
        default=Path(".local/kobo-scans"),
        help="Where each scan is recorded, one appendable file per month.",
    )
    kobo_repair.add_argument(
        "--log-folder",
        default=os.environ.get("GOOGLE_DRIVE_FOLDER_DB", ""),
        help="Drive folder the scan log is copied to, so the record is not only local.",
    )
    kobo_repair.add_argument(
        "--no-log", action="store_true", help="Scan without recording anything."
    )
    kobo_repair.add_argument(
        "--backup",
        type=Path,
        default=Path(".local/kobo-backups"),
        help="Where the damaged originals are copied before anything is rewritten.",
    )

    rights_audit.add_argument(
        "--within",
        type=int,
        help=(
            "Print only the ids of Sources expired, unreviewed, or expiring within this many "
            "days, one per line — the workflow's escalation branch."
        ),
    )
    return parser


def _report_failure(run_id: str, code: str, message: str) -> None:
    print(f"run_id={run_id} code={code} message={message}", file=sys.stderr)


def _load_or_report(config: Path, run_id: str) -> tuple[bool, int]:
    try:
        parsed = load_config(config)
    except ConfigError as error:
        _report_failure(run_id, error.code, error.safe_message)
        return False, 0
    return True, len(parsed.publications)


def _validate(config: Path) -> int:
    run_id = create_run_id()
    valid, publication_count = _load_or_report(config, run_id)
    if not valid:
        return 2
    print(f"run_id={run_id} code=CONFIG_VALID publications={publication_count}")
    return 0


def _parse_time(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(UTC)


def _generate(arguments: argparse.Namespace) -> int:
    run_id = arguments.run_id or create_run_id()
    if not _RUN_ID.fullmatch(run_id):
        _report_failure(create_run_id(), "RUN_ID_INVALID", "Run ID is invalid")
        return 2
    try:
        generated_at = _parse_time(arguments.at)
    except ValueError:
        _report_failure(run_id, "GENERATION_TIME_INVALID", "Generation time is invalid")
        return 2
    try:
        configuration = load_config(arguments.config)
    except ConfigError as error:
        _report_failure(run_id, error.code, error.safe_message)
        return 2
    diagnostics = arguments.diagnostics or arguments.state.parent / "diagnostics"
    drive_folder = arguments.drive_folder or os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
    archive_folder = arguments.archive_folder or os.environ.get("GOOGLE_DRIVE_FOLDER_ARCHIVE")
    state_folder = arguments.state_folder or os.environ.get("GOOGLE_DRIVE_FOLDER_DB")
    drive_target = None
    state_sync_target = None
    if drive_folder or state_folder:
        try:
            credentials = credentials_from_environment()
        except DriveConfigurationError as error:
            _report_failure(run_id, "DRIVE_CONFIGURATION_INVALID", str(error))
            return 2
        client = HttpDriveClient(credentials=credentials)
        if drive_folder:
            drive_target = DriveTarget(
                client=client, folder_id=drive_folder, archive_folder_id=archive_folder
            )
        if state_folder:
            state_sync_target = StateSyncTarget(
                client=client, folder_id=state_folder, environment=arguments.state_environment
            )
    try:
        result = generate_edition(
            configuration,
            state_path=arguments.state,
            output_directory=arguments.output,
            diagnostics_directory=diagnostics,
            run_id=run_id,
            generated_at=generated_at,
            publication_id=arguments.publication,
            epubcheck_jar=arguments.epubcheck_jar,
            drive_target=drive_target,
            state_sync_target=state_sync_target,
        )
    except GenerationError as error:
        _report_failure(run_id, error.code, error.safe_message)
        return 3
    except Exception:
        _report_failure(run_id, "GENERATION_FAILED", "Edition generation failed")
        return 3
    print(
        f"run_id={run_id} code=EDITION_DELIVERED articles={result.article_count} "
        f"briefs={result.brief_count} read_items={result.read_item_count} "
        f"partial={str(result.partial).lower()}"
    )
    return 0


def _ollama_check(host: str, model: str) -> int:
    run_id = create_run_id()
    try:
        check_ollama(host=host, model=model)
    except OllamaError as error:
        _report_failure(run_id, "OLLAMA_UNAVAILABLE", str(error))
        return 3
    print(f"run_id={run_id} code=OLLAMA_READY model={model}")
    return 0


def _open_url(url: str) -> None:
    print(f"Open this URL to authorize (or it may open automatically): {url}")
    with suppress(Exception):
        webbrowser.open(url)


def _authorize_drive(client_secret_path: Path | None) -> int:
    try:
        path = client_secret_path or find_client_secret(Path.cwd())
        client_secret = load_client_secret(path)
        refresh_token = authorize(client_secret, open_url=_open_url)
    except DriveAuthorizationError as error:
        print(f"code=DRIVE_AUTHORIZATION_FAILED message={error}", file=sys.stderr)
        return 3
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={refresh_token}")
    print("Store this value as a GitHub secret; this command has not saved it anywhere.")
    return 0


def _state_pull(state_path: Path, folder_id: str, environment: str) -> int:
    """Debug command: restore the State Store from Drive, fail-closed like ``generate`` does."""

    try:
        credentials = credentials_from_environment()
    except DriveConfigurationError as error:
        print(f"code=DRIVE_CONFIGURATION_INVALID message={error}", file=sys.stderr)
        return 2
    client = HttpDriveClient(credentials=credentials)
    try:
        outcome = restore_state(
            client=client, folder_id=folder_id, state_path=state_path, environment=environment
        )
    except StateSyncAuthError as error:
        print(f"code=DRIVE_AUTH_FAILED message={error}", file=sys.stderr)
        return 3
    except StateSyncError as error:
        print(f"code=STATE_RESTORE_FAILED message={error}", file=sys.stderr)
        return 3
    if outcome.restored:
        print(f"code=STATE_RESTORED state={state_path}")
    else:
        print(f"code=STATE_ABSENT state={state_path}")
    return 0


def _state_push(state_path: Path, folder_id: str, environment: str) -> int:
    """Debug command: save the State Store to Drive, overwriting the archive in place."""

    try:
        credentials = credentials_from_environment()
    except DriveConfigurationError as error:
        print(f"code=DRIVE_CONFIGURATION_INVALID message={error}", file=sys.stderr)
        return 2
    client = HttpDriveClient(credentials=credentials)
    try:
        digest = save_state(
            client=client, folder_id=folder_id, state_path=state_path, environment=environment
        )
    except StateSyncAuthError as error:
        print(f"code=DRIVE_AUTH_FAILED message={error}", file=sys.stderr)
        return 3
    except StateSyncError as error:
        print(f"code=STATE_SAVE_FAILED message={error}", file=sys.stderr)
        return 3
    print(f"code=STATE_SAVED digest={digest}")
    return 0


def _source_health_row(record: SourceHealth) -> tuple[str, str, int, str]:
    last_success = (
        "never" if record.last_success is None else record.last_success.date().isoformat()
    )
    return (
        record.source_id,
        record.response_classification,
        record.consecutive_failures,
        last_success,
    )


def _source_health_text(records: Sequence[SourceHealth]) -> str:
    lines = [
        f"source_id={source_id} classification={classification} "
        f"consecutive_failures={failures} last_success={last_success}"
        for source_id, classification, failures, last_success in (
            _source_health_row(record) for record in records
        )
    ]
    return "\n".join(lines)


def _markdown_cell(value: str) -> str:
    """Neutralize GFM table syntax: the summary is world-readable, and a value carrying a
    pipe or a newline must not break the layout or spoof rows outside its own cell."""

    return value.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _source_health_markdown(records: Sequence[SourceHealth]) -> str:
    # A source_id and a classification code, never a title or a URL: this is meant for
    # $GITHUB_STEP_SUMMARY on a public repository, matching the diagnostic report's convention.
    lines = [
        "| Source | Classification | Consecutive Failures | Last Success |",
        "| --- | --- | --- | --- |",
    ]
    for source_id, classification, failures, last_success in (
        _source_health_row(record) for record in records
    ):
        marker = "⚠️ " if failures >= 3 else ""
        lines.append(
            f"| {marker}{_markdown_cell(source_id)} | {_markdown_cell(classification)} "
            f"| {failures} | {last_success} |"
        )
    return "\n".join(lines)


def _source_health(state_path: Path, output_format: str) -> int:
    """Always exits 0: a report surfaces Source health, it does not gate the Edition.

    That promise has to survive a corrupt file too — the workflow step runs under
    ``set -e`` after a delivered Edition, and a report must never turn that delivery red.
    """

    try:
        records = read_source_health(state_path)
    except (sqlite3.Error, OSError, ValueError):
        print("Source health could not be read; the Edition is unaffected.")
        return 0
    if not records:
        print("No Source health has been recorded yet.")
        return 0
    ordered = sorted(records, key=lambda record: (-record.consecutive_failures, record.source_id))
    if output_format == "markdown":
        print(_source_health_markdown(ordered))
    else:
        print(_source_health_text(ordered))
    return 0


# Inside this horizon a Source is marked urgent in the audit table. Matches the in-run
# Publication Note window in application.py so every surface starts warning together.
_AUDIT_WARNING_DAYS = 14


def _rights_horizons(config: Path, as_of: date) -> list[tuple[str, str, int | None]] | None:
    """Per Source: (source_id, expiry label, days left) — None days for a Source never reviewed.

    Sorted most urgent first: never-reviewed and expired Sources lead (a Source without
    evidence is already ineligible today), then the soonest expiry, then source_id.
    """

    try:
        configuration = load_config(config)
    except ConfigError as error:
        print(f"code={error.code} message={error.safe_message}", file=sys.stderr)
        return None
    horizons: list[tuple[str, str, int | None]] = []
    for source_id, source in configuration.sources.items():
        if source.eligibility is None:
            horizons.append((source_id, "never-reviewed", None))
            continue
        expires = source.eligibility.review_expires_at
        horizons.append((source_id, expires.isoformat(), (expires - as_of).days))
    horizons.sort(
        key=lambda row: (row[2] if row[2] is not None else -(10**6), row[0]),
    )
    return horizons


def _rights_audit(arguments: argparse.Namespace) -> int:
    """Always exits 0 when the configuration loads: a report, never a gate.

    Observed live (2026-09-09): every Source shared one review_expires_at, the whole fleet
    fail-closed on a single morning, and nothing had warned. This surfaces the horizon on
    the surfaces the operator already reads; the gate itself stays untouched.
    """

    try:
        as_of = date.fromisoformat(arguments.at) if arguments.at else datetime.now(UTC).date()
    except ValueError:
        print("code=AUDIT_DATE_INVALID message=Audit date is invalid", file=sys.stderr)
        return 2
    horizons = _rights_horizons(arguments.config, as_of)
    if horizons is None:
        return 2
    if arguments.within is not None:
        for source_id, _expires, days_left in horizons:
            if days_left is None or days_left <= arguments.within:
                print(source_id)
        return 0
    if arguments.format == "markdown":
        lines = [
            "| Source | Review expires | Days left |",
            "| --- | --- | --- |",
        ]
        for source_id, expires, days_left in horizons:
            urgent = days_left is None or days_left <= _AUDIT_WARNING_DAYS
            marker = "⚠️ " if urgent else ""
            lines.append(
                f"| {marker}{_markdown_cell(source_id)} | {expires} "
                f"| {'—' if days_left is None else days_left} |"
            )
        print("\n".join(lines))
        return 0
    for source_id, expires, days_left in horizons:
        rendered_days = "never" if days_left is None else str(days_left)
        print(f"source_id={source_id} expires={expires} days_left={rendered_days}")
    return 0


def _kobo_repair(arguments: argparse.Namespace) -> int:
    """Report, and on request repair, Drive downloads a Kobo prefixed with an error body.

    Reporting is the default because the repair writes to the device. Nothing is rewritten
    until Drive has confirmed the payload digest, so the command cannot invent bytes.
    """

    try:
        root = drive_download_root(arguments.volume)
    except KoboRepairError as error:
        print(f"code=KOBO_VOLUME_INVALID message={error}", file=sys.stderr)
        return 2
    damaged = damaged_downloads(root)
    if not damaged:
        print(f"code=KOBO_DOWNLOADS_INTACT volume={arguments.volume}")
        _record_scan(arguments, damaged, ())
        return 0
    for download in damaged:
        print(
            f"code=KOBO_DOWNLOAD_DAMAGED name={download.name} "
            f"prefix_bytes={download.prefix_bytes} sha256={download.payload_sha256}"
        )
    if not arguments.apply:
        print(f"code=KOBO_REPAIR_AVAILABLE damaged={len(damaged)} hint=--apply")
        _record_scan(arguments, damaged, ())
        return 0
    folders = arguments.drive_folder or [
        folder
        for folder in (
            os.environ.get("GOOGLE_DRIVE_FOLDER_ID"),
            os.environ.get("GOOGLE_DRIVE_FOLDER_ARCHIVE"),
        )
        if folder
    ]
    if not folders:
        print(
            "code=KOBO_REPAIR_UNVERIFIABLE message=--drive-folder is required to apply a repair",
            file=sys.stderr,
        )
        return 2
    try:
        credentials = credentials_from_environment()
    except DriveConfigurationError as error:
        print(f"code=DRIVE_CONFIGURATION_INVALID message={error}", file=sys.stderr)
        return 2
    client = HttpDriveClient(credentials=credentials)
    outcomes = repair_downloads(
        damaged,
        drive_digests=_drive_digests(client, folders),
        backup_directory=arguments.backup,
    )
    for outcome in outcomes:
        if outcome.repaired:
            code = "KOBO_DOWNLOAD_REPAIRED"
        elif outcome.uncertain:
            code = "KOBO_DOWNLOAD_UNCERTAIN"
        else:
            code = "KOBO_DOWNLOAD_UNCHANGED"
        backup = f" backup={outcome.backup}" if outcome.backup else ""
        print(f"code={code} name={outcome.name} reason={outcome.reason}{backup}")
    _record_scan(arguments, damaged, outcomes)
    return 0 if all(outcome.repaired for outcome in outcomes) else 3


def _record_scan(
    arguments: argparse.Namespace,
    damaged: Sequence[object],
    outcomes: Sequence[object],
) -> None:
    """Append this scan to the local log and copy it to Drive, never failing the scan.

    Nobody has a measured failure rate for the Kobo download corruption, including Kobo. One
    record per visit is what turns "it seems intermittent" into an answer, so the log is written
    whether or not anything was damaged — a clean scan is a data point too.
    """

    if arguments.no_log:
        return
    record = scan_record(
        volume=arguments.volume,
        damaged=cast(Any, damaged),
        outcomes=cast(Any, outcomes),
        at=datetime.now(UTC),
    )
    try:
        path = append_scan(record, log_directory=arguments.log_dir)
    except OSError as error:
        print(f"code=KOBO_SCAN_LOG_FAILED message={error}", file=sys.stderr)
        return
    print(f"code=KOBO_SCAN_LOGGED path={path}")
    if not arguments.log_folder:
        return
    try:
        client = HttpDriveClient(credentials=credentials_from_environment())
        existing = client.find_file(folder_id=arguments.log_folder, filename=path.name)
        body = path.read_bytes()
        if existing is None:
            client.upload(
                folder_id=arguments.log_folder,
                filename=path.name,
                content=body,
                content_type="application/x-ndjson",
            )
        else:
            client.update(
                file_id=existing.file_id, content=body, content_type="application/x-ndjson"
            )
    except Exception as error:  # the scan happened and the local record stands
        print(f"code=KOBO_SCAN_UPLOAD_FAILED message={error}", file=sys.stderr)
        return
    print(f"code=KOBO_SCAN_UPLOADED folder={arguments.log_folder} name={path.name}")


def _drive_digests(
    client: HttpDriveClient, folder_ids: Sequence[str]
) -> Callable[[str], tuple[str, ...]]:
    """Answer with the digest of every file Drive holds under that name, across all folders.

    Every configured folder is consulted rather than stopping at the first hit, because the
    delivery folder and the archive can hold the same name with different bytes; stopping early
    would refuse a legitimate repair of the archived Edition. Drive's own ``md5Checksum`` is not
    used: the repair has to prove the on-device payload is the Edition that was delivered, and
    only the bytes themselves prove that.
    """

    def digests(name: str) -> tuple[str, ...]:
        found: list[str] = []
        failures: list[Exception] = []
        for folder_id in folder_ids:
            try:
                match = client.find_file(folder_id=folder_id, filename=name)
                if match is not None:
                    found.append(sha256(client.download(file_id=match.file_id)).hexdigest())
            except Exception as error:  # a folder we cannot reach must not veto one we can
                failures.append(error)
        if not found and failures:
            # An outage is not the same answer as "Drive does not hold it"; reporting it as
            # absence would read as a mismatch and hide the real problem.
            raise failures[0]
        return tuple(found)

    return digests


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "generate":
        return _generate(arguments)
    if arguments.command == "validate":
        return _validate(arguments.config)
    if arguments.command == "ollama-check":
        return _ollama_check(arguments.host, arguments.model)
    if arguments.command == "authorize-drive":
        return _authorize_drive(arguments.client_secret)
    if arguments.command == "state-pull":
        return _state_pull(arguments.state, arguments.state_folder, arguments.state_environment)
    if arguments.command == "state-push":
        return _state_push(arguments.state, arguments.state_folder, arguments.state_environment)
    if arguments.command == "source-health":
        return _source_health(arguments.state, arguments.format)
    if arguments.command == "rights-audit":
        return _rights_audit(arguments)
    if arguments.command == "kobo-repair":
        return _kobo_repair(arguments)
    raise AssertionError(f"Unhandled command: {arguments.command}")
