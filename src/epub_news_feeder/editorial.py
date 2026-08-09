"""Provider-neutral gate for optional, locally generated editorial additions."""

from __future__ import annotations

import hashlib
import json
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
    "generate_editorial",
]


class ArticleEvidence(StrictModel):
    """Allowlisted publisher evidence supplied to both local model roles."""

    article_id: NonEmptyString
    title: NonEmptyString
    publisher: NonEmptyString
    canonical_url: NonEmptyString
    published_at: NonEmptyString
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
    "Write short article summaries using only the supplied evidence. Return strict JSON. "
    "Every sentence must cite one or more supplied article_id values. Do not use outside knowledge."
)
_VERIFIER_SYSTEM_PROMPT = (
    "Independently classify every proposed sentence against only the supplied article evidence as "
    "supported, unsupported, or uncertain. Return strict JSON and do not repair prose."
)
_REPAIR_SYSTEM_PROMPT = (
    "Repair only sentences classified unsupported or uncertain using only the supplied evidence. "
    "Return the complete proposal as strict JSON with citations. Do not use outside knowledge."
)


def generate_editorial(
    evidence: tuple[ArticleEvidence, ...],
    model_pair: ModelPair,
    provider: StructuredProvider,
) -> EditorialResult:
    """Return only independently supported additions; omit editorial output on failure."""

    if not evidence or model_pair.editorial_model == model_pair.verifier_model:
        return _empty_result(model_pair, 0)

    calls = 0
    evidence_findings: list[EvidenceFinding] = []
    try:
        proposal_call = StructuredCall(
            role="editorial",
            model=model_pair.editorial_model,
            prompt_version=model_pair.editorial_prompt_version,
            system_prompt=_EDITORIAL_SYSTEM_PROMPT,
            input={"articles": [item.model_dump(mode="json") for item in evidence]},
            response_schema=_EditorialProposal.model_json_schema(),
        )
        calls += 1
        proposal = _EditorialProposal.model_validate(provider.complete(proposal_call))
        _validate_citations(proposal, evidence)

        verification_call = _verification_call(
            evidence=evidence, proposal=proposal, model_pair=model_pair
        )
        calls += 1
        verification = _VerificationResponse.model_validate(provider.complete(verification_call))
        _validate_finding_coverage(proposal, verification)
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
                response_schema=_EditorialProposal.model_json_schema(),
            )
            calls += 1
            proposal = _EditorialProposal.model_validate(provider.complete(repair_call))
            _validate_citations(proposal, evidence)

            fresh_verification_call = _verification_call(
                evidence=evidence, proposal=proposal, model_pair=model_pair
            )
            calls += 1
            verification = _VerificationResponse.model_validate(
                provider.complete(fresh_verification_call)
            )
            _validate_finding_coverage(proposal, verification)
            evidence_findings.extend(_evidence_findings(verification, verification_round=2))
            if any(finding.status != "supported" for finding in verification.findings):
                return _empty_result(model_pair, calls, evidence_findings)

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
            ),
        )
    except Exception:
        return _empty_result(model_pair, calls, evidence_findings)


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
) -> EditorialResult:
    return EditorialResult(
        additions=[],
        evidence=LLMEvidenceRecord(
            status="omitted", calls=calls, model_pair=model_pair, findings=findings or []
        ),
    )
