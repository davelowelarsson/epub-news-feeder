from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from epub_news_feeder import __version__
from epub_news_feeder.application import GenerationError, generate_edition
from epub_news_feeder.config import ConfigError, load_config
from epub_news_feeder.ollama import OllamaError, check_ollama
from epub_news_feeder.run_id import create_run_id

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

    validate = commands.add_parser("validate", help="Validate configuration without side effects.")
    validate.add_argument("--config", required=True, type=Path)

    ollama = commands.add_parser(
        "ollama-check", help="Verify a local Ollama model and strict JSON output."
    )
    ollama.add_argument("--host", default="http://127.0.0.1:11434")
    ollama.add_argument("--model", required=True)
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


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "generate":
        return _generate(arguments)
    if arguments.command == "validate":
        return _validate(arguments.config)
    if arguments.command == "ollama-check":
        return _ollama_check(arguments.host, arguments.model)
    raise AssertionError(f"Unhandled command: {arguments.command}")
