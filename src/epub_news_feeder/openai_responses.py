"""Narrow OpenAI Responses adapter for the remote editorial route.

The same ``StructuredProvider`` boundary Ollama implements, with one difference that
governs the whole module: this one sends publisher text off the operator's machine. Every
choice here is made to keep that transfer as small, as short-lived, and as explicitly
authorised as possible.

- ``store: false`` — the response is not persisted to the account.
- ``prompt_cache_retention: "in_memory"`` — the default is a 24-hour prompt cache; an
  Edition that reaches one Source per morning gains nothing from it and it is the only
  remaining server-side copy of the prompt once ``store`` is off.
- ``tools: []`` — no retrieval, no web access, no function calls, matching the ``tools:
  "none"`` a recorded provider profile must declare before this adapter is ever built.
- The origin is pinned. There is no configurable base URL, so a mistyped host cannot send
  Articles somewhere the recorded eligibility decision never covered.

Strict schema mode is deliberately not used: the schemas are Pydantic-derived and carry
``minItems``/``minLength``, which strict mode rejects. The schema is still sent as the
response format, restated in the system prompt, and — the actual gate — every response is
validated against the Pydantic model by ``editorial.py``, which omits rather than repairs
anything that does not conform.
"""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING

import httpx

from .editorial import CallUsage, StructuredProviderError

if TYPE_CHECKING:
    from .editorial import StructuredCall

_ENDPOINT = "https://api.openai.com/v1/responses"
_API_KEY_VARIABLE = "OPENAI_API_KEY"


class OpenAIError(StructuredProviderError):
    """Safe remote provider failure; never carries the API key or any response body."""


class OpenAIResponsesProvider:
    """Remote implementation of the optional editorial provider boundary."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 120,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get(_API_KEY_VARIABLE, "")
        if not _valid_api_key(key):
            raise OpenAIError("An OpenAI API key is required for the remote editorial route")
        self._api_key = key
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
                response = client.post(
                    _ENDPOINT,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": call.model,
                        "store": False,
                        "prompt_cache_retention": "in_memory",
                        "tools": [],
                        "input": [
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
                                        call.input, ensure_ascii=False, separators=(",", ":")
                                    )
                                ),
                            },
                        ],
                        "text": {
                            "format": {
                                "type": "json_schema",
                                "name": f"{call.role}_response",
                                "schema": call.response_schema,
                            }
                        },
                    },
                )
                response.raise_for_status()
                content = _structured_content(response)
                self._usage.append(_call_usage(call, response))
                return content
        except OpenAIError:
            raise
        except (
            httpx.HTTPError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            AttributeError,
        ) as error:
            raise OpenAIError("OpenAI structured request failed") from error

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self._timeout, transport=self._transport, trust_env=False)


def _valid_api_key(key: str) -> bool:
    """Reject an absent or malformed key here rather than leaking it into a request."""

    return re.fullmatch(r"sk-[A-Za-z0-9._\-]{16,255}", key) is not None


def _validate_model_name(model: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,127}", model) is None:
        raise OpenAIError("OpenAI model identifier is invalid")


def _call_usage(call: StructuredCall, response: httpx.Response) -> CallUsage:
    """Read the provider's own token counters; absent counters measure as zero.

    ``load_duration_ms`` has no remote equivalent and stays zero, so one diagnostic shape
    describes both routes rather than each provider inventing its own.
    """

    payload = response.json()
    usage = payload.get("usage") if isinstance(payload, dict) else None
    counters = usage if isinstance(usage, dict) else {}
    return CallUsage(
        role=call.role,
        model=call.model,
        total_duration_ms=0,
        load_duration_ms=0,
        input_tokens=_counter(counters.get("input_tokens")),
        output_tokens=_counter(counters.get("output_tokens")),
    )


def _counter(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _structured_content(response: httpx.Response) -> dict[str, object]:
    """Extract the single JSON object from the Responses output, refusing anything else.

    A refusal content part, a tool call, or an incomplete response are all failures rather
    than something to salvage: a partial summary is worse than no summary.
    """

    result = response.json()
    if not isinstance(result, dict) or result.get("status") != "completed":
        raise OpenAIError("OpenAI returned invalid structured output")
    output = result.get("output")
    if not isinstance(output, list):
        raise OpenAIError("OpenAI returned invalid structured output")
    texts = [
        part["text"]
        for item in output
        if isinstance(item, dict) and item.get("type") == "message"
        for part in item.get("content", [])
        if isinstance(part, dict)
        and part.get("type") == "output_text"
        and isinstance(part.get("text"), str)
    ]
    if len(texts) != 1:
        raise OpenAIError("OpenAI returned invalid structured output")
    content = texts[0]
    fenced = re.fullmatch(r"```json\s*(\{.*\})\s*```", content, flags=re.DOTALL)
    if fenced is not None:
        content = fenced.group(1)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as error:
        raise OpenAIError("OpenAI returned invalid structured output") from error
    if not isinstance(parsed, dict):
        raise OpenAIError("OpenAI returned invalid structured output")
    return parsed


def _schema_instruction(system_prompt: str, response_schema: dict[str, object]) -> str:
    return (
        f"{system_prompt}\n\n"
        "Return exactly one JSON object matching this JSON Schema. Do not include Markdown, prose, "
        "explanations, or extra fields. Schema:\n"
        + json.dumps(response_schema, ensure_ascii=False, separators=(",", ":"))
    )
