from __future__ import annotations

from collections.abc import Iterable

import pytest

from epub_news_feeder.editorial import (
    ArticleEvidence,
    ModelPair,
    StructuredCall,
    StructuredProviderError,
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
            language="en",
            lead_passage="Rescue crews continued searching on Sunday.",
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


def _words(count: int) -> str:
    """Build an English-markered string of exactly `count` whitespace-separated words."""

    return " ".join([f"filler{i}" for i in range(count - 1)] + ["the"])


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
    proposal_def = provider.calls[0].response_schema["$defs"]["_ProposedSummary"]  # type: ignore[index]
    sentence_def = provider.calls[0].response_schema["$defs"]["CitedSentence"]  # type: ignore[index]
    assert proposal_def["properties"]["article_id"]["enum"] == ["article-1"]
    assert sentence_def["properties"]["citations"]["items"]["enum"] == ["article-1"]
    assert provider.calls[0].input["articles"][0]["language"] == "en"  # type: ignore[index]
    assert "do not translate" in provider.calls[0].system_prompt
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
def test_summary_at_exactly_the_word_ceiling_is_accepted_unchanged() -> None:
    at_ceiling = _words(120)
    provider = FakeProvider(
        [
            {
                "summaries": [
                    {
                        "article_id": "article-1",
                        "sentences": [{"text": at_ceiling, "citations": ["article-1"]}],
                    }
                ]
            },
            {"findings": [{"summary_index": 0, "sentence_index": 0, "status": "supported"}]},
        ]
    )

    result = generate_editorial(_evidence(), _models(), provider)

    assert result.evidence.status == "accepted"
    assert result.evidence.calls == 2
    assert result.additions[0].sentences[0].text == at_ceiling


@pytest.mark.editorial
def test_over_ceiling_summary_is_classified_unsupported_and_repaired_under_ceiling() -> None:
    over_ceiling = {
        "summaries": [
            {
                "article_id": "article-1",
                "sentences": [{"text": _words(121), "citations": ["article-1"]}],
            }
        ]
    }
    under_ceiling = {
        "summaries": [
            {
                "article_id": "article-1",
                "sentences": [{"text": _words(50), "citations": ["article-1"]}],
            }
        ]
    }
    provider = FakeProvider(
        [
            over_ceiling,
            # The real verifier finds nothing wrong; the deterministic ceiling must still fire.
            {"findings": [{"summary_index": 0, "sentence_index": 0, "status": "supported"}]},
            under_ceiling,
            {"findings": [{"summary_index": 0, "sentence_index": 0, "status": "supported"}]},
        ]
    )

    result = generate_editorial(_evidence(), _models(), provider)

    assert result.evidence.status == "accepted"
    assert result.evidence.calls == 4
    assert result.additions[0].sentences[0].text == _words(50)
    assert [finding.status for finding in result.evidence.findings] == ["unsupported", "supported"]
    assert [call.role for call in provider.calls] == [
        "editorial",
        "verifier",
        "editorial",
        "verifier",
    ]


@pytest.mark.editorial
def test_repair_still_over_word_ceiling_omits_that_articles_addition() -> None:
    over_ceiling = {
        "summaries": [
            {
                "article_id": "article-1",
                "sentences": [{"text": _words(121), "citations": ["article-1"]}],
            }
        ]
    }
    still_over_ceiling = {
        "summaries": [
            {
                "article_id": "article-1",
                "sentences": [{"text": _words(130), "citations": ["article-1"]}],
            }
        ]
    }
    provider = FakeProvider(
        [
            over_ceiling,
            {"findings": [{"summary_index": 0, "sentence_index": 0, "status": "supported"}]},
            still_over_ceiling,
            {"findings": [{"summary_index": 0, "sentence_index": 0, "status": "supported"}]},
            AssertionError("a third editorial attempt must never happen"),
        ]
    )

    result = generate_editorial(_evidence(), _models(), provider)

    assert result.additions == []
    assert result.evidence.status == "omitted"
    assert result.evidence.failure_code == "verification_rejected"
    assert result.evidence.calls == 4
    assert [finding.status for finding in result.evidence.findings] == [
        "unsupported",
        "unsupported",
    ]
    assert len(provider.calls) == 4


@pytest.mark.editorial
def test_word_ceiling_counts_the_whole_summary_not_each_sentence() -> None:
    over_ceiling_split_across_sentences = {
        "summaries": [
            {
                "article_id": "article-1",
                "sentences": [
                    {"text": _words(70), "citations": ["article-1"]},
                    {"text": _words(70), "citations": ["article-1"]},
                ],
            }
        ]
    }
    under_ceiling = {
        "summaries": [
            {
                "article_id": "article-1",
                "sentences": [{"text": _words(50), "citations": ["article-1"]}],
            }
        ]
    }
    provider = FakeProvider(
        [
            over_ceiling_split_across_sentences,
            {
                "findings": [
                    {"summary_index": 0, "sentence_index": 0, "status": "supported"},
                    {"summary_index": 0, "sentence_index": 1, "status": "supported"},
                ]
            },
            under_ceiling,
            {"findings": [{"summary_index": 0, "sentence_index": 0, "status": "supported"}]},
        ]
    )

    result = generate_editorial(_evidence(), _models(), provider)

    assert result.evidence.status == "accepted"
    assert result.evidence.calls == 4
    assert [finding.status for finding in result.evidence.findings[:2]] == [
        "unsupported",
        "unsupported",
    ]


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
def test_failure_evidence_distinguishes_provider_from_invalid_model_output() -> None:
    from epub_news_feeder.editorial import StructuredProviderError

    provider_failure = generate_editorial(
        _evidence(), _models(), FakeProvider([StructuredProviderError("unavailable")])
    )
    invalid_output = generate_editorial(_evidence(), _models(), FakeProvider([{"summaries": []}]))

    assert provider_failure.evidence.failure_code == "provider_failure"
    assert invalid_output.evidence.failure_code == "invalid_model_output"


@pytest.mark.editorial
def test_wrong_language_summary_is_rejected_before_verifier_call() -> None:
    provider = FakeProvider(
        [
            {
                "summaries": [
                    {
                        "article_id": "article-1",
                        "sentences": [
                            {
                                "text": (
                                    "Artikeln beskriver räddningsarbetet och de personer "
                                    "som fördes i land."
                                ),
                                "citations": ["article-1"],
                            }
                        ],
                    }
                ]
            },
            AssertionError("wrong-language prose must not reach the verifier"),
        ]
    )

    result = generate_editorial(_evidence(), _models(), provider)

    assert result.additions == []
    assert result.evidence.failure_code == "invalid_model_output"
    assert result.evidence.calls == 1
    assert len(provider.calls) == 1


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


@pytest.mark.editorial
def test_evidence_records_one_wall_clock_duration_for_every_provider_call() -> None:
    provider = FakeProvider(
        [
            {
                "summaries": [
                    {
                        "article_id": "article-1",
                        "sentences": [
                            {
                                "text": "Two people were brought ashore during the search.",
                                "citations": ["article-1"],
                            }
                        ],
                    }
                ]
            },
            {"findings": [{"summary_index": 0, "sentence_index": 0, "status": "supported"}]},
        ]
    )

    result = generate_editorial(_evidence(), _models(), provider)

    assert result.evidence.status == "accepted"
    assert len(result.evidence.call_durations_ms) == result.evidence.calls == 2
    assert all(duration >= 0 for duration in result.evidence.call_durations_ms)


@pytest.mark.editorial
def test_evidence_records_durations_for_calls_made_before_a_provider_failure() -> None:
    provider = FakeProvider([StructuredProviderError("provider is down")])

    result = generate_editorial(_evidence(), _models(), provider)

    assert result.evidence.status == "omitted"
    assert result.evidence.failure_code == "provider_failure"
    assert len(result.evidence.call_durations_ms) == result.evidence.calls == 1
