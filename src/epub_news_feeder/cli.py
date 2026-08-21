from __future__ import annotations

import argparse
import os
import re
import sys
import webbrowser
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

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
            drive_target = DriveTarget(client=client, folder_id=drive_folder)
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
        lines.append(f"| {marker}{source_id} | {classification} | {failures} | {last_success} |")
    return "\n".join(lines)


def _source_health(state_path: Path, output_format: str) -> int:
    """Always exits 0: a report surfaces Source health, it does not gate the Edition."""

    records = read_source_health(state_path)
    if not records:
        print("No Source health has been recorded yet.")
        return 0
    ordered = sorted(records, key=lambda record: (-record.consecutive_failures, record.source_id))
    if output_format == "markdown":
        print(_source_health_markdown(ordered))
    else:
        print(_source_health_text(ordered))
    return 0


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
    raise AssertionError(f"Unhandled command: {arguments.command}")
