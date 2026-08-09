from __future__ import annotations

from collections.abc import Iterable

import pytest

from epub_news_feeder.editorial import (
    ArticleEvidence,
    ModelPair,
    StructuredCall,
    generate_editorial,
)


class FakeProvider:
    def __init__(self, responses: Iterable[object]) -> None:
        self._responses = iter(responses)
        self.calls: list[StructuredCall] = []

    def complete(self, call: StructuredCall) -> object:
        self.calls.append(call)
        response = next(self._responses)
        if isinstance(response, Exception):
            raise response
        return response


def _evidence() -> tuple[ArticleEvidence, ...]:
    return (
        ArticleEvidence(
            article_id="article-1",
            title="Rescue operation continues",
            publisher="Example News",
            canonical_url="https://example.test/rescue",
            published_at="2026-08-09T07:30:00Z",
            body="Rescue crews continued searching on Sunday. Two people were brought ashore.",
        ),
    )


def _models() -> ModelPair:
    return ModelPair(
        editorial_model="gemma4:12b-mlx",
        verifier_model="gemma4:e4b-mlx",
        editorial_prompt_version="article-summary-v1",
        verifier_prompt_version="claim-verifier-v1",
        schema_version=1,
    )


@pytest.mark.editorial
def test_accepts_labelled_summary_when_every_sentence_is_independently_supported() -> None:
    provider = FakeProvider(
        [
            {
                "summaries": [
                    {
                        "article_id": "article-1",
                        "sentences": [
                            {
                                "text": "Rescue crews continued searching on Sunday.",
                                "citations": ["article-1"],
                            },
                            {
                                "text": "Two people were brought ashore.",
                                "citations": ["article-1"],
                            },
                        ],
                    }
                ]
            },
            {
                "findings": [
                    {"summary_index": 0, "sentence_index": 0, "status": "supported"},
                    {"summary_index": 0, "sentence_index": 1, "status": "supported"},
                ]
            },
        ]
    )

    result = generate_editorial(_evidence(), _models(), provider)

    assert [addition.model_dump() for addition in result.additions] == [
        {
            "article_id": "article-1",
            "label": "AI-generated summary",
            "sentences": [
                {
                    "text": "Rescue crews continued searching on Sunday.",
                    "citations": ["article-1"],
                },
                {"text": "Two people were brought ashore.", "citations": ["article-1"]},
            ],
        }
    ]
    assert result.evidence.status == "accepted"
    assert result.evidence.calls == 2
    assert result.evidence.model_pair == _models()
    assert [(call.role, call.model) for call in provider.calls] == [
        ("editorial", "gemma4:12b-mlx"),
        ("verifier", "gemma4:e4b-mlx"),
    ]
    assert all(call.tools == () and call.web_access is False for call in provider.calls)
    assert all(call.response_schema["additionalProperties"] is False for call in provider.calls)
    retained = result.evidence.model_dump_json()
    assert _evidence()[0].body not in retained
    assert provider.calls[0].system_prompt not in retained
    assert provider.calls[1].system_prompt not in retained


@pytest.mark.editorial
def test_repairs_once_then_runs_a_fresh_verification_before_accepting() -> None:
    first_proposal = {
        "summaries": [
            {
                "article_id": "article-1",
                "sentences": [
                    {
                        "text": "Three people were brought ashore.",
                        "citations": ["article-1"],
                    }
                ],
            }
        ]
    }
    repaired_proposal = {
        "summaries": [
            {
                "article_id": "article-1",
                "sentences": [
                    {
                        "text": "Two people were brought ashore.",
                        "citations": ["article-1"],
                    }
                ],
            }
        ]
    }
    provider = FakeProvider(
        [
            first_proposal,
            {"findings": [{"summary_index": 0, "sentence_index": 0, "status": "unsupported"}]},
            repaired_proposal,
            {"findings": [{"summary_index": 0, "sentence_index": 0, "status": "supported"}]},
        ]
    )

    result = generate_editorial(_evidence(), _models(), provider)

    assert result.evidence.status == "accepted"
    assert result.evidence.calls == 4
    assert result.additions[0].sentences[0].text == "Two people were brought ashore."
    assert [call.role for call in provider.calls] == [
        "editorial",
        "verifier",
        "editorial",
        "verifier",
    ]


@pytest.mark.editorial
@pytest.mark.parametrize("final_status", ["unsupported", "uncertain"])
def test_omits_after_one_failed_repair_without_a_third_editorial_attempt(
    final_status: str,
) -> None:
    proposal = {
        "summaries": [
            {
                "article_id": "article-1",
                "sentences": [{"text": "An ungrounded claim.", "citations": ["article-1"]}],
            }
        ]
    }
    provider = FakeProvider(
        [
            proposal,
            {"findings": [{"summary_index": 0, "sentence_index": 0, "status": "uncertain"}]},
            proposal,
            {"findings": [{"summary_index": 0, "sentence_index": 0, "status": final_status}]},
            AssertionError("a third editorial attempt must never happen"),
        ]
    )

    result = generate_editorial(_evidence(), _models(), provider)

    assert result.additions == []
    assert result.evidence.status == "omitted"
    assert result.evidence.calls == 4
    assert [finding.status for finding in result.evidence.findings] == [
        "uncertain",
        final_status,
    ]
    assert len(provider.calls) == 4


@pytest.mark.editorial
@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("provider unavailable"),
        {"summaries": [], "unexpected": "not allowed"},
        {
            "summaries": [
                {
                    "article_id": "article-1",
                    "sentences": [{"text": "A claim.", "citations": ["article-not-supplied"]}],
                }
            ]
        },
    ],
)
def test_provider_and_schema_failures_return_the_same_body_free_fallback(failure: object) -> None:
    secret_body = "PRIVATE-ARTICLE-BODY-DO-NOT-RETAIN"
    evidence = (_evidence()[0].model_copy(update={"body": secret_body}),)
    provider = FakeProvider([failure])

    result = generate_editorial(evidence, _models(), provider)

    assert result.additions == []
    assert result.evidence.status == "omitted"
    retained = result.evidence.model_dump_json()
    assert secret_body not in retained
    assert "Write short article summaries" not in retained
    assert "Independently classify" not in retained


@pytest.mark.editorial
def test_no_article_evidence_skips_models_and_returns_empty_fallback() -> None:
    provider = FakeProvider([AssertionError("provider must not be called")])

    result = generate_editorial((), _models(), provider)

    assert result.additions == []
    assert result.evidence.status == "omitted"
    assert result.evidence.calls == 0
    assert provider.calls == []


@pytest.mark.editorial
def test_same_model_cannot_propose_and_independently_verify_its_own_summary() -> None:
    provider = FakeProvider([AssertionError("provider must not be called")])
    model_pair = _models().model_copy(update={"verifier_model": "gemma4:12b-mlx"})

    result = generate_editorial(_evidence(), model_pair, provider)

    assert result.additions == []
    assert result.evidence.status == "omitted"
    assert result.evidence.calls == 0
    assert provider.calls == []
