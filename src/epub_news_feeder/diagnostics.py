"""Body-free structured run diagnostics."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_ALLOWED_FIELDS = {
    "archived",
    "article_id",
    "articles",
    "briefs",
    "calls",
    "clusters",
    "digest",
    "duration_ms",
    "evidence_id",
    "input_characters",
    "input_tokens",
    "omitted",
    "outcome",
    "output_tokens",
    "partial",
    "provider",
    "publication_id",
    "read_items",
    "reason",
    "route",
    "source_id",
}


class Diagnostics:
    """Append safe, structured events to a private JSONL file."""

    def __init__(self, directory: Path, run_id: str, *, retention_days: int = 90) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            directory.chmod(0o700)
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        for candidate in directory.glob("*.jsonl"):
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=UTC)
            if modified < cutoff:
                candidate.unlink(missing_ok=True)
        self.path = directory / f"{run_id}.jsonl"
        self.summary_path = directory / "outcomes.ndjson"

    def emit(self, code: str, *, phase: str, **fields: str | int | bool | None) -> None:
        if not set(fields).issubset(_ALLOWED_FIELDS):
            raise ValueError("Diagnostic field is not allowlisted")
        event: dict[str, Any] = {"run_id": self.path.stem, "phase": phase, "code": code}
        event.update({key: value for key, value in fields.items() if value is not None})
        self._append(self.path, event)
        if phase == "delivery" or (phase == "run" and fields.get("outcome") == "failed"):
            aggregate = {
                key: value
                for key, value in event.items()
                if key
                in {
                    "run_id",
                    "phase",
                    "code",
                    "articles",
                    "briefs",
                    "read_items",
                    "partial",
                    "outcome",
                }
            }
            self._append(self.summary_path, aggregate)

    @staticmethod
    def _append(path: Path, event: dict[str, Any]) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o600)
