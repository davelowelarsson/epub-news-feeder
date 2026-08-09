"""Provider-neutral gate for optional, locally generated editorial additions."""

from __future__ import annotations

import hashlib
import json
import re
import time
from copy import deepcopy
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .models import ModelPair, NonEmptyString, StrictModel

__all__ = [
    "ArticleEvidence",
    "EditorialAddition",
    "EditorialResult",
    "ModelPair",
    "StructuredCall",
    "StructuredProvider",
    "StructuredProviderError",
    "generate_editorial",
]


class StructuredProviderError(Exception):
    """Provider transport or structured-response failure safe for classification."""


class ArticleEvidence(StrictModel):
    """Allowlisted publisher evidence supplied to both local model roles."""

    article_id: NonEmptyString
    title: NonEmptyString
    publisher: NonEmptyString
    canonical_url: NonEmptyString
    published_at: NonEmptyString
    language: NonEmptyString
    lead_passage: NonEmptyString
    body: NonEmptyString


class CitedSentence(StrictModel):
    text: NonEmptyString
    citations: list[NonEmptyString] = Field(min_length=1)


class _ProposedSummary(StrictModel):
    article_id: NonEmptyString
    sentences: list[CitedSentence] = Field(min_length=1, max_length=5)


class _EditorialProposal(StrictModel):
    summaries: list[_ProposedSummary] = Field(min_length=1)


class _VerificationFinding(StrictModel):
    summary_index: int = Field(ge=0)
    sentence_index: int = Field(ge=0)
    status: Literal["supported", "unsupported", "uncertain"]


class _VerificationResponse(StrictModel):
    findings: list[_VerificationFinding] = Field(min_length=1)


class EditorialAddition(StrictModel):
    """Reader-facing generated prose, explicitly distinct from journalism."""

    article_id: NonEmptyString
    label: Literal["AI-generated summary"] = "AI-generated summary"
    sentences: list[CitedSentence]


class EvidenceFinding(StrictModel):
    """Body-free finding retained for evaluation of a Model Pair."""

    verification_round: Literal[1, 2]
    summary_index: int
    sentence_index: int
    status: Literal["supported", "unsupported", "uncertain"]


class LLMEvidenceRecord(StrictModel):
    """Bounded evidence record; deliberately excludes prompts and Article bodies."""

    status: Literal["accepted", "omitted"]
    calls: int = Field(ge=0, le=4)
    model_pair: ModelPair
    proposal_sha256: str | None = None
    findings: list[EvidenceFinding] = Field(default_factory=list)
    call_durations_ms: list[int] = Field(default_factory=list)
    failure_code: (
        Literal["provider_failure", "invalid_model_output", "verification_rejected"] | None
    ) = None


class EditorialResult(StrictModel):
    additions: list[EditorialAddition]
    evidence: LLMEvidenceRecord


class StructuredCall(StrictModel):
    """One local structured-output call at the provider boundary."""

    role: Literal["editorial", "verifier"]
    model: NonEmptyString
    prompt_version: NonEmptyString
    system_prompt: NonEmptyString
    input: dict[str, object]
    response_schema: dict[str, object]
    tools: tuple[str, ...] = ()
    web_access: Literal[False] = False

    @model_validator(mode="after")
    def forbid_tools(self) -> StructuredCall:
        if self.tools:
            raise ValueError("editorial calls cannot use tools")
        return self


class StructuredProvider(Protocol):
    """Injected boundary implemented by a local structured-output provider."""

    def complete(self, call: StructuredCall) -> object: ...


_EDITORIAL_SYSTEM_PROMPT = (
    "Write short article summaries using only the supplied evidence. Write each summary in its "
    "Article language; do not translate. Add useful orientation beyond the supplied lead_passage "
    "and do not copy a complete publisher sentence. Omit a summary when no non-redundant value is "
    "possible. Return strict JSON. Every sentence must cite one or more supplied article_id "
    "values. Do not use outside knowledge."
)
_VERIFIER_SYSTEM_PROMPT = (
    "Independently classify every proposed sentence against only the supplied article evidence as "
    "supported, unsupported, or uncertain. A sentence is unsupported when it uses a language other "
    "than that Article's language, copies a complete publisher sentence, or merely repeats the "
    "lead without adding useful orientation. Return strict JSON and do not repair prose."
)
_REPAIR_SYSTEM_PROMPT = (
    "Repair only sentences classified unsupported or uncertain using only the supplied evidence. "
    "Use each Article's language, avoid complete publisher sentences, and add useful orientation "
    "beyond its lead_passage. Return the complete proposal as strict JSON with citations. Do not "
    "use outside knowledge."
)


def _timed(provider: StructuredProvider, call: StructuredCall, durations: list[int]) -> object:
    """Measure one provider call's wall clock, whether it returns or raises."""

    started = time.monotonic()
    try:
        return provider.complete(call)
    finally:
        durations.append(max(0, round((time.monotonic() - started) * 1000)))


def generate_editorial(
    evidence: tuple[ArticleEvidence, ...],
    model_pair: ModelPair,
    provider: StructuredProvider,
) -> EditorialResult:
    """Return only independently supported additions; omit editorial output on failure."""

    if not evidence or model_pair.editorial_model == model_pair.verifier_model:
        return _empty_result(model_pair, 0, durations=[])

    calls = 0
    durations: list[int] = []
    evidence_findings: list[EvidenceFinding] = []
    try:
        proposal_call = StructuredCall(
            role="editorial",
            model=model_pair.editorial_model,
            prompt_version=model_pair.editorial_prompt_version,
            system_prompt=_EDITORIAL_SYSTEM_PROMPT,
            input={"articles": [item.model_dump(mode="json") for item in evidence]},
            response_schema=_proposal_schema(evidence),
        )
        calls += 1
        proposal = _EditorialProposal.model_validate(_timed(provider, proposal_call, durations))
        _validate_citations(proposal, evidence)
        _validate_summary_languages(proposal, evidence)

        verification_call = _verification_call(
            evidence=evidence, proposal=proposal, model_pair=model_pair
        )
        calls += 1
        verification = _VerificationResponse.model_validate(
            _timed(provider, verification_call, durations)
        )
        _validate_finding_coverage(proposal, verification)
        verification = _enforce_word_ceiling(proposal, verification)
        evidence_findings.extend(_evidence_findings(verification, verification_round=1))
        if any(finding.status != "supported" for finding in verification.findings):
            repair_call = StructuredCall(
                role="editorial",
                model=model_pair.editorial_model,
                prompt_version=model_pair.editorial_prompt_version,
                system_prompt=_REPAIR_SYSTEM_PROMPT,
                input={
                    "articles": [item.model_dump(mode="json") for item in evidence],
                    "proposal": proposal.model_dump(mode="json"),
                    "findings": verification.model_dump(mode="json")["findings"],
                },
                response_schema=_proposal_schema(evidence),
            )
            calls += 1
            proposal = _EditorialProposal.model_validate(_timed(provider, repair_call, durations))
            _validate_citations(proposal, evidence)
            _validate_summary_languages(proposal, evidence)

            fresh_verification_call = _verification_call(
                evidence=evidence, proposal=proposal, model_pair=model_pair
            )
            calls += 1
            verification = _VerificationResponse.model_validate(
                _timed(provider, fresh_verification_call, durations)
            )
            _validate_finding_coverage(proposal, verification)
            verification = _enforce_word_ceiling(proposal, verification)
            evidence_findings.extend(_evidence_findings(verification, verification_round=2))
            if any(finding.status != "supported" for finding in verification.findings):
                return _empty_result(
                    model_pair,
                    calls,
                    evidence_findings,
                    failure_code="verification_rejected",
                    durations=durations,
                )

        serialized = json.dumps(
            proposal.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return EditorialResult(
            additions=[
                EditorialAddition(article_id=item.article_id, sentences=item.sentences)
                for item in proposal.summaries
            ],
            evidence=LLMEvidenceRecord(
                status="accepted",
                calls=calls,
                model_pair=model_pair,
                proposal_sha256=hashlib.sha256(serialized).hexdigest(),
                findings=evidence_findings,
                call_durations_ms=durations,
            ),
        )
    except StructuredProviderError:
        return _empty_result(
            model_pair,
            calls,
            evidence_findings,
            failure_code="provider_failure",
            durations=durations,
        )
    except Exception:
        return _empty_result(
            model_pair,
            calls,
            evidence_findings,
            failure_code="invalid_model_output",
            durations=durations,
        )


def _proposal_schema(evidence: tuple[ArticleEvidence, ...]) -> dict[str, object]:
    schema = deepcopy(_EditorialProposal.model_json_schema())
    article_ids = [item.article_id for item in evidence]
    definitions = schema["$defs"]
    assert isinstance(definitions, dict)
    proposal = definitions["_ProposedSummary"]
    sentence = definitions["CitedSentence"]
    assert isinstance(proposal, dict) and isinstance(sentence, dict)
    proposal["properties"]["article_id"]["enum"] = article_ids
    sentence["properties"]["citations"]["items"]["enum"] = article_ids
    return schema


def _verification_call(
    *,
    evidence: tuple[ArticleEvidence, ...],
    proposal: _EditorialProposal,
    model_pair: ModelPair,
) -> StructuredCall:
    return StructuredCall(
        role="verifier",
        model=model_pair.verifier_model,
        prompt_version=model_pair.verifier_prompt_version,
        system_prompt=_VERIFIER_SYSTEM_PROMPT,
        input={
            "articles": [item.model_dump(mode="json") for item in evidence],
            "proposal": proposal.model_dump(mode="json"),
        },
        response_schema=_VerificationResponse.model_json_schema(),
    )


def _evidence_findings(
    verification: _VerificationResponse, *, verification_round: Literal[1, 2]
) -> list[EvidenceFinding]:
    return [
        EvidenceFinding(
            verification_round=verification_round,
            summary_index=item.summary_index,
            sentence_index=item.sentence_index,
            status=item.status,
        )
        for item in verification.findings
    ]


def _validate_citations(
    proposal: _EditorialProposal, evidence: tuple[ArticleEvidence, ...]
) -> None:
    article_ids = {item.article_id for item in evidence}
    summary_ids: set[str] = set()
    for summary in proposal.summaries:
        if summary.article_id not in article_ids or summary.article_id in summary_ids:
            raise ValueError("proposal references unavailable or repeated Article evidence")
        summary_ids.add(summary.article_id)
        for sentence in summary.sentences:
            if not set(sentence.citations).issubset(article_ids):
                raise ValueError("sentence citation references unavailable Article evidence")


_LANGUAGE_MARKERS = {
    "en": frozenset(
        {
            "a",
            "an",
            "and",
            "are",
            "as",
            "at",
            "by",
            "for",
            "from",
            "has",
            "in",
            "is",
            "of",
            "on",
            "that",
            "the",
            "this",
            "to",
            "was",
            "were",
            "which",
            "will",
            "with",
        }
    ),
    "sv": frozenset(
        {
            "att",
            "av",
            "de",
            "den",
            "det",
            "en",
            "ett",
            "för",
            "från",
            "har",
            "i",
            "inte",
            "med",
            "och",
            "på",
            "som",
            "till",
            "vilket",
            "visar",
            "är",
        }
    ),
}


def _validate_summary_languages(
    proposal: _EditorialProposal, evidence: tuple[ArticleEvidence, ...]
) -> None:
    languages = {item.article_id: item.language.casefold().split("-", 1)[0] for item in evidence}
    for summary in proposal.summaries:
        expected = languages[summary.article_id]
        if expected not in _LANGUAGE_MARKERS:
            raise ValueError("summary language cannot be verified deterministically")
        tokens = re.findall(
            r"[^\W\d_]+", " ".join(sentence.text for sentence in summary.sentences).casefold()
        )
        scores = {
            language: sum(token in markers for token in tokens)
            for language, markers in _LANGUAGE_MARKERS.items()
        }
        competing = max(score for language, score in scores.items() if language != expected)
        if scores[expected] == 0 or scores[expected] <= competing:
            raise ValueError("summary does not match the Article language")


_SUMMARY_WORD_CEILING = 60


def _summary_word_count(summary: _ProposedSummary) -> int:
    """Count words over the summary as a whole; sentence boundaries do not reset the count."""

    return len(" ".join(sentence.text for sentence in summary.sentences).split())


def _enforce_word_ceiling(
    proposal: _EditorialProposal, verification: _VerificationResponse
) -> _VerificationResponse:
    """Force every sentence of an over-ceiling summary unsupported, feeding the repair round."""

    over_ceiling = {
        index
        for index, summary in enumerate(proposal.summaries)
        if _summary_word_count(summary) > _SUMMARY_WORD_CEILING
    }
    if not over_ceiling:
        return verification
    return _VerificationResponse(
        findings=[
            (
                _VerificationFinding(
                    summary_index=finding.summary_index,
                    sentence_index=finding.sentence_index,
                    status="unsupported",
                )
                if finding.summary_index in over_ceiling
                else finding
            )
            for finding in verification.findings
        ]
    )


def _validate_finding_coverage(
    proposal: _EditorialProposal, verification: _VerificationResponse
) -> None:
    expected = {
        (summary_index, sentence_index)
        for summary_index, summary in enumerate(proposal.summaries)
        for sentence_index, _sentence in enumerate(summary.sentences)
    }
    actual = {(item.summary_index, item.sentence_index) for item in verification.findings}
    if actual != expected or len(actual) != len(verification.findings):
        raise ValueError("verification findings do not cover every sentence exactly once")


def _empty_result(
    model_pair: ModelPair,
    calls: int,
    findings: list[EvidenceFinding] | None = None,
    failure_code: Literal["provider_failure", "invalid_model_output", "verification_rejected"]
    | None = None,
    durations: list[int] | None = None,
) -> EditorialResult:
    return EditorialResult(
        additions=[],
        evidence=LLMEvidenceRecord(
            status="omitted",
            calls=calls,
            model_pair=model_pair,
            findings=findings or [],
            failure_code=failure_code,
            call_durations_ms=durations or [],
        ),
    )
