from __future__ import annotations

import json

import httpx
import pytest

from epub_news_feeder.editorial import StructuredCall
from epub_news_feeder.ollama import OllamaError, OllamaStructuredProvider


def _call(*, model: str = "gemma4:12b-mlx") -> StructuredCall:
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


@pytest.mark.contract
def test_local_provider_validates_the_exact_installed_model_and_returns_json() -> None:
    def ollama(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma4:12b-mlx"}]})
        assert request.url.path == "/api/chat"
        payload = json.loads(request.content)
        assert payload == {
            "model": "gemma4:12b-mlx",
            "stream": False,
            "think": False,
            "format": _call().response_schema,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Use only supplied evidence.\n\n"
                        "Return exactly one JSON object matching this JSON Schema. Do not include "
                        "Markdown, prose, explanations, or extra fields. Schema:\n"
                        '{"type":"object","properties":{"summaries":{"type":"array"}},'
                        '"required":["summaries"],"additionalProperties":false}'
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Return only JSON conforming to the requested schema. Input:\n"
                        '{"articles":[{"article_id":"article-1","title":"A title"}]}'
                    ),
                },
            ],
            "options": {"temperature": 0},
        }
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": '{"summaries": []}'}},
        )

    provider = OllamaStructuredProvider(
        host="http://127.0.0.1:11434", transport=httpx.MockTransport(ollama)
    )

    assert provider.complete(_call()) == {"summaries": []}


@pytest.mark.security
def test_local_provider_rejects_non_loopback_hosts() -> None:
    with pytest.raises(OllamaError, match="local loopback"):
        OllamaStructuredProvider(host="http://ollama.example.test:11434")


@pytest.mark.security
def test_local_provider_rejects_missing_models_without_sending_content() -> None:
    def ollama(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "gemma4:e4b-mlx"}]})

    provider = OllamaStructuredProvider(
        host="http://127.0.0.1:11434", transport=httpx.MockTransport(ollama)
    )

    with pytest.raises(OllamaError, match="not installed"):
        provider.complete(_call())


@pytest.mark.security
def test_local_provider_sanitizes_malformed_structured_output() -> None:
    def ollama(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma4:12b-mlx"}]})
        return httpx.Response(200, json={"message": {"content": "not json"}})

    provider = OllamaStructuredProvider(
        host="http://127.0.0.1:11434", transport=httpx.MockTransport(ollama)
    )

    with pytest.raises(OllamaError, match="invalid structured output"):
        provider.complete(_call())


@pytest.mark.contract
def test_local_provider_normalizes_gemma_json_fence_without_accepting_prose() -> None:
    def ollama(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma4:12b-mlx"}]})
        return httpx.Response(
            200,
            json={"message": {"content": '```json\n{"summaries": []}\n```'}},
        )

    provider = OllamaStructuredProvider(
        host="http://127.0.0.1:11434", transport=httpx.MockTransport(ollama)
    )

    assert provider.complete(_call()) == {"summaries": []}
