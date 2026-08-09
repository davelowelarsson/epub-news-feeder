"""Narrow local Ollama adapter with a structured-output readiness probe."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from .editorial import StructuredProviderError

if TYPE_CHECKING:
    from .editorial import StructuredCall


class OllamaError(StructuredProviderError):
    """Safe local provider failure."""


@dataclass(frozen=True, slots=True)
class CallUsage:
    """Body-free measurement of one local structured call."""

    role: str
    model: str
    total_duration_ms: int
    load_duration_ms: int
    input_tokens: int
    output_tokens: int


_READY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"status": {"type": "string", "const": "ok"}},
    "required": ["status"],
    "additionalProperties": False,
}


class OllamaStructuredProvider:
    """Local-only implementation of the optional editorial provider boundary."""

    def __init__(
        self,
        *,
        host: str = "http://127.0.0.1:11434",
        timeout: float = 120,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._host = _local_host(host)
        self._timeout = timeout
        self._transport = transport
        self._usage: list[CallUsage] = []

    def drain_usage(self) -> tuple[CallUsage, ...]:
        """Return measurements recorded since the previous drain, and clear them."""

        drained = tuple(self._usage)
        self._usage.clear()
        return drained

    def complete(self, call: StructuredCall) -> object:
        """Return one parsed JSON response, or a safe provider error."""

        _validate_model_name(call.model)
        try:
            with self._client() as client:
                _require_installed_model(client, call.model)
                response = client.post(
                    "/api/chat",
                    json={
                        "model": call.model,
                        "stream": False,
                        "think": False,
                        "format": call.response_schema,
                        "messages": [
                            {
                                "role": "system",
                                "content": _schema_instruction(
                                    call.system_prompt, call.response_schema
                                ),
                            },
                            {
                                "role": "user",
                                "content": (
                                    "Return only JSON conforming to the requested schema. Input:\n"
                                    + json.dumps(
                                        call.input,
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    )
                                ),
                            },
                        ],
                        "options": {"temperature": 0},
                    },
                )
                response.raise_for_status()
                content = _structured_content(response)
                self._usage.append(_call_usage(call, response))
                return content
        except OllamaError:
            raise
        except (
            httpx.HTTPError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            AttributeError,
        ) as error:
            raise OllamaError("Ollama structured request failed") from error

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self._host,
            timeout=self._timeout,
            transport=self._transport,
            trust_env=False,
        )


def check_ollama(*, host: str, model: str, timeout: float = 120) -> None:
    """Prove the named model exists and obeys a minimal strict JSON contract."""

    base_url = _local_host(host)
    _validate_model_name(model)
    try:
        with httpx.Client(base_url=base_url, timeout=timeout, trust_env=False) as client:
            _require_installed_model(client, model)
            response = client.post(
                "/api/chat",
                json={
                    "model": model,
                    "stream": False,
                    "think": False,
                    "format": _READY_SCHEMA,
                    "messages": [
                        {
                            "role": "system",
                            "content": _schema_instruction(
                                "Follow the readiness instruction exactly.", _READY_SCHEMA
                            ),
                        },
                        {
                            "role": "user",
                            "content": "Return JSON with status set to ok. No other fields.",
                        },
                    ],
                    "options": {"temperature": 0, "num_predict": 256},
                },
            )
            response.raise_for_status()
            if _structured_content(response) != {"status": "ok"}:
                raise OllamaError("Ollama returned invalid structured output")
    except OllamaError:
        raise
    except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError, AttributeError) as error:
        raise OllamaError("Ollama readiness check failed") from error


def _local_host(host: str) -> str:
    """Accept only an origin addressed directly by a loopback IP literal."""

    parsed = urlsplit(host)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise OllamaError("Ollama host must be a local loopback origin")
    try:
        address = ip_address(parsed.hostname)
    except ValueError as error:
        raise OllamaError("Ollama host must be a local loopback origin") from error
    if not address.is_loopback:
        raise OllamaError("Ollama host must be a local loopback origin")
    return host.rstrip("/")


def _validate_model_name(model: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:+\-]{0,127}", model) is None:
        raise OllamaError("Ollama model identifier is invalid")


def _require_installed_model(client: httpx.Client, model: str) -> None:
    tags = client.get("/api/tags")
    tags.raise_for_status()
    payload = tags.json()
    if not isinstance(payload, dict):
        raise OllamaError("Ollama model inventory is invalid")
    models = payload.get("models")
    if not isinstance(models, list):
        raise OllamaError("Ollama model inventory is invalid")
    names = {item.get("name") for item in models if isinstance(item, dict)}
    if model not in names:
        raise OllamaError("The requested Ollama model is not installed")


def _call_usage(call: StructuredCall, response: httpx.Response) -> CallUsage:
    """Read Ollama's own timing and token counters; absent counters measure as zero."""

    payload = response.json()
    metadata = payload if isinstance(payload, dict) else {}
    return CallUsage(
        role=call.role,
        model=call.model,
        total_duration_ms=_nanoseconds_to_ms(metadata.get("total_duration")),
        load_duration_ms=_nanoseconds_to_ms(metadata.get("load_duration")),
        input_tokens=_counter(metadata.get("prompt_eval_count")),
        output_tokens=_counter(metadata.get("eval_count")),
    )


def _nanoseconds_to_ms(value: object) -> int:
    return _counter(value) // 1_000_000


def _counter(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _structured_content(response: httpx.Response) -> dict[str, object]:
    result = response.json()
    if not isinstance(result, dict) or not isinstance(result.get("message"), dict):
        raise OllamaError("Ollama returned invalid structured output")
    content = result["message"].get("content")
    if not isinstance(content, str):
        raise OllamaError("Ollama returned invalid structured output")
    fenced = re.fullmatch(r"```json\s*(\{.*\})\s*```", content, flags=re.DOTALL)
    if fenced is not None:
        content = fenced.group(1)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as error:
        raise OllamaError("Ollama returned invalid structured output") from error
    if not isinstance(parsed, dict):
        raise OllamaError("Ollama returned invalid structured output")
    return parsed


def _schema_instruction(system_prompt: str, response_schema: dict[str, object]) -> str:
    return (
        f"{system_prompt}\n\n"
        "Return exactly one JSON object matching this JSON Schema. Do not include Markdown, prose, "
        "explanations, or extra fields. Schema:\n"
        + json.dumps(response_schema, ensure_ascii=False, separators=(",", ":"))
    )
