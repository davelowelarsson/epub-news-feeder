"""Body-free structured run diagnostics."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any


class Diagnostics:
    """Append safe, structured events to a private JSONL file."""

    def __init__(self, directory: Path, run_id: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            directory.chmod(0o700)
        self.path = directory / f"{run_id}.jsonl"

    def emit(self, code: str, *, phase: str, **fields: str | int | bool | None) -> None:
        event: dict[str, Any] = {"run_id": self.path.stem, "phase": phase, "code": code}
        event.update({key: value for key, value in fields.items() if value is not None})
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if not self.path.exists():
                os.close(descriptor)
