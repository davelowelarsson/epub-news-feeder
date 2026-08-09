from __future__ import annotations

import secrets
from datetime import UTC, datetime

_BASE32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def create_run_id(*, now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = "".join(secrets.choice(_BASE32) for _ in range(8))
    return f"{timestamp}-{suffix}"
