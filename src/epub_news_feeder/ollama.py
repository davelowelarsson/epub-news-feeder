"""Narrow local Ollama adapter with a structured-output readiness probe."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx


class OllamaError(Exception):
    """Safe local provider failure."""


_READY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"status": {"type": "string", "const": "ok"}},
    "required": ["status"],
    "additionalProperties": False,
}


def check_ollama(*, host: str, model: str, timeout: float = 120) -> None:
    """Prove the named model exists and obeys a minimal strict JSON contract."""

    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:+\-]{0,127}", model) is None:
        raise OllamaError("Ollama model identifier is invalid")
    try:
        with httpx.Client(base_url=host.rstrip("/"), timeout=timeout) as client:
            tags = client.get("/api/tags")
            tags.raise_for_status()
            payload = tags.json()
            names = {
                item.get("name") for item in payload.get("models", []) if isinstance(item, dict)
            }
            if model not in names:
                raise OllamaError("The requested Ollama model is not installed")
            response = client.post(
                "/api/chat",
                json={
                    "model": model,
                    "stream": False,
                    "think": False,
                    "format": _READY_SCHEMA,
                    "messages": [
                        {
                            "role": "user",
                            "content": "Return JSON with status set to ok. No other fields.",
                        }
                    ],
                    "options": {"temperature": 0, "num_predict": 256},
                },
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or not isinstance(result.get("message"), dict):
                raise OllamaError("Ollama returned invalid structured output")
            content = result["message"].get("content")
            if not isinstance(content, str) or json.loads(content) != {"status": "ok"}:
                raise OllamaError("Ollama returned invalid structured output")
    except OllamaError:
        raise
    except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError, AttributeError) as error:
        raise OllamaError("Ollama readiness check failed") from error
