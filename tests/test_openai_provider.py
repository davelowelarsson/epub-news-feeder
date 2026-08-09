from __future__ import annotations

import json

import httpx
import pytest

from epub_news_feeder.editorial import StructuredCall
from epub_news_feeder.openai_responses import OpenAIError, OpenAIResponsesProvider

API_KEY = "sk-test-0123456789abcdefghijklmnop"


def _call(*, model: str = "gpt-5.4-2026-03-05") -> StructuredCall:
    return StructuredCall(
        role="editorial",
        model=model,
        prompt_version="v1",
        system_prompt="Use only supplied evidence.",
        input={"articles": [{"article_id": "article-1", "title": "A title"}]},
        response_schema={
            "type": "object",
            "properties": {"summaries": {"type": "array"}},
            "required": ["summaries"],
            "additionalProperties": False,
        },
    )


def _completed(text: str, *, usage: dict[str, int] | None = None) -> dict[str, object]:
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": usage or {"input_tokens": 800, "output_tokens": 60},
    }


def _provider(handler: object) -> OpenAIResponsesProvider:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return OpenAIResponsesProvider(api_key=API_KEY, transport=transport)


@pytest.mark.contract
@pytest.mark.security
def test_remote_provider_never_stores_never_caches_and_offers_no_tools() -> None:
    """The four privacy properties of the request are asserted on the wire, not on intent."""

    def openai(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        payload = json.loads(request.content)
        assert payload["store"] is False
        assert payload["prompt_cache_retention"] == "in_memory"
        assert payload["tools"] == []
        assert payload["model"] == "gpt-5.4-2026-03-05"
        assert payload["text"]["format"]["type"] == "json_schema"
        assert payload["text"]["format"]["schema"] == _call().response_schema
        assert [message["role"] for message in payload["input"]] == ["system", "user"]
        assert "Use only supplied evidence." in payload["input"][0]["content"]
        return httpx.Response(200, json=_completed('{"summaries":[]}'))

    assert _provider(openai).complete(_call()) == {"summaries": []}


@pytest.mark.contract
def test_remote_provider_records_the_providers_own_token_counters() -> None:
    def openai(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_completed('{"summaries":[]}', usage={"input_tokens": 1234, "output_tokens": 77}),
        )

    provider = _provider(openai)
    provider.complete(_call())
    usage = provider.drain_usage()

    assert [(item.role, item.input_tokens, item.output_tokens) for item in usage] == [
        ("editorial", 1234, 77)
    ]
    assert provider.drain_usage() == (), "a drain must not return the same measurement twice"


@pytest.mark.contract
@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("incomplete", {"status": "incomplete", "output": []}),
        (
            "refusal only",
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "I cannot help"}],
                    }
                ],
            },
        ),
        (
            "two messages",
            {
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "{}"}]},
                    {"type": "message", "content": [{"type": "output_text", "text": "{}"}]},
                ],
            },
        ),
        (
            "not json",
            {
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "sorry"}]}
                ],
            },
        ),
        (
            "json but not an object",
            {
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "[1,2]"}]}
                ],
            },
        ),
    ],
)
def test_remote_provider_refuses_anything_but_one_complete_json_object(
    name: str, payload: dict[str, object]
) -> None:
    """A partial or refused response is a failure, never something to salvage."""

    def openai(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(OpenAIError):
        _provider(openai).complete(_call())


@pytest.mark.contract
def test_remote_provider_turns_a_transport_failure_into_a_safe_error() -> None:
    def openai(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    with pytest.raises(OpenAIError) as failure:
        _provider(openai).complete(_call())
    assert API_KEY not in str(failure.value)


@pytest.mark.security
@pytest.mark.parametrize("key", ["", "not-a-key", "sk-short"])
def test_remote_provider_refuses_to_build_without_a_usable_key(
    key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail before a request exists, so a missing key can never be sent as an empty bearer."""

    monkeypatch.setenv("OPENAI_API_KEY", key)
    with pytest.raises(OpenAIError) as failure:
        OpenAIResponsesProvider()
    assert key not in str(failure.value) or key == ""


@pytest.mark.security
def test_remote_provider_reads_the_key_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)

    def openai(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        return httpx.Response(200, json=_completed('{"summaries":[]}'))

    provider = OpenAIResponsesProvider(transport=httpx.MockTransport(openai))
    assert provider.complete(_call()) == {"summaries": []}


@pytest.mark.security
def test_remote_provider_rejects_an_invalid_model_identifier_before_calling() -> None:
    def openai(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("an invalid model must not reach the network")

    with pytest.raises(OpenAIError):
        _provider(openai).complete(_call(model="../../etc/passwd"))
