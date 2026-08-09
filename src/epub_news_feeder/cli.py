from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from epub_news_feeder import __version__
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
    return parser


def _report_failure(run_id: str, code: str, message: str) -> None:
    print(f"run_id={run_id} code={code} message={message}", file=sys.stderr)


def _generate(config: Path) -> int:
    run_id = create_run_id()
    if not config.is_file():
        _report_failure(run_id, "CONFIG_NOT_FOUND", "Configuration file not found")
        return 2
    _report_failure(run_id, "NOT_IMPLEMENTED", "Generation is not implemented")
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "generate":
        return _generate(arguments.config)
    raise AssertionError(f"Unhandled command: {arguments.command}")
