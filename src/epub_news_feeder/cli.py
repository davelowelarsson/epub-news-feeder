from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from epub_news_feeder import __version__
from epub_news_feeder.config import ConfigError, load_config
from epub_news_feeder.run_id import create_run_id


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

    validate = commands.add_parser("validate", help="Validate configuration without side effects.")
    validate.add_argument("--config", required=True, type=Path)
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


def _generate(config: Path) -> int:
    run_id = create_run_id()
    valid, _ = _load_or_report(config, run_id)
    if not valid:
        return 2
    _report_failure(run_id, "NOT_IMPLEMENTED", "Generation is not implemented")
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "generate":
        return _generate(arguments.config)
    if arguments.command == "validate":
        return _validate(arguments.config)
    raise AssertionError(f"Unhandled command: {arguments.command}")
