from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, model_validator

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
Weight = Annotated[int, Field(ge=1, le=10)]
Fraction = Annotated[float, Field(ge=0.0, le=1.0)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class Budget(StrictModel):
    max_articles: PositiveInt | None = None
    min_articles: NonNegativeInt | None = None
    weight: Weight | None = None


class MatchRule(StrictModel):
    field: Literal["title", "category", "body"]
    match: Literal["phrase", "word"] = "phrase"
    value: NonEmptyString
    weight: Weight | None = None


class MuteRule(StrictModel):
    source: NonEmptyString | None = None
    rule: MatchRule | None = None
    expires_at: date | None = None

    @model_validator(mode="after")
    def require_one_match(self) -> MuteRule:
        if (self.source is None) == (self.rule is None):
            raise ValueError("a mute rule needs exactly one match")
        return self


class PolicyPreset(StrictModel):
    type: Literal["coverage", "interest"]
    source_weights: dict[NonEmptyString, Weight] = Field(default_factory=dict)
    essential_coverage: list[MatchRule] = Field(default_factory=list)
    positive_rules: list[MatchRule] = Field(default_factory=list)
    negative_rules: list[MatchRule] = Field(default_factory=list)
    mute_rules: list[MuteRule] = Field(default_factory=list)
    minimum_sources: PositiveInt = 2
    single_source_cap: Fraction = 0.6
    discovery_slice: Fraction = 0.2
    full_body_matching: bool = False


class RightsPolicy(StrictModel):
    basis: NonEmptyString
    audience: Literal["single_operator"]
    attribution_required: bool
    media_reuse: bool


class EligibilityEvidence(StrictModel):
    evidence_reviewed_at: date
    review_expires_at: date
    evidence_id: NonEmptyString
    feed_acquisition: Literal["allow", "deny", "conditional", "unknown"] = "unknown"
    page_acquisition: Literal["allow", "deny", "conditional", "unknown"] = "unknown"
    retention: Literal["allow", "deny", "conditional", "unknown"] = "unknown"
    private_distribution: Literal["allow", "deny", "conditional", "unknown"] = "unknown"
    local_llm: Literal["allow", "deny", "conditional", "unknown"] = "unknown"
    remote_llm: Literal["allow", "deny", "conditional", "unknown"] = "unknown"

    @model_validator(mode="after")
    def expiry_follows_review(self) -> EligibilityEvidence:
        if self.review_expires_at < self.evidence_reviewed_at:
            raise ValueError("eligibility evidence expires before review")
        return self


class Source(StrictModel):
    title: NonEmptyString
    publisher_id: NonEmptyString | None = None
    copyright_notice: NonEmptyString | None = None
    allowed_publisher_origins: list[HttpUrl] = Field(default_factory=list)
    feed_url: HttpUrl
    acquisition: Literal["auto", "feed", "web", "metadata_only"] = "auto"
    weight: Weight = 5
    llm_processing: Literal["disabled", "local_only", "remote_allowed"] = "local_only"
    rights: RightsPolicy | None = None
    eligibility: EligibilityEvidence | None = None


class RemoteProviderProfile(StrictModel):
    training_opt_in: bool
    store: bool
    application_state_retention_days: NonNegativeInt | None = None
    max_abuse_retention_days: NonNegativeInt
    region: NonEmptyString | None = None
    tools: Literal["none"]
    subprocessors: list[NonEmptyString] | None = None


class ModelPair(StrictModel):
    editorial_model: NonEmptyString
    verifier_model: NonEmptyString
    editorial_prompt_version: NonEmptyString
    verifier_prompt_version: NonEmptyString
    schema_version: PositiveInt


class CostEnvelope(StrictModel):
    max_calls: PositiveInt
    max_tokens: PositiveInt
    max_cost: Annotated[float, Field(ge=0.0)] | None = None


class EditorialConfig(StrictModel):
    enabled: bool = False
    influence: Literal["none", "tie_break", "bounded", "editorial"] = "none"
    remote_processing: bool = False
    provider: NonEmptyString | None = None
    model_pair: ModelPair | None = None
    capabilities: list[
        Literal["ranking", "clustering", "article_summary", "revision_summary", "section_overview"]
    ] = Field(default_factory=list)
    cost_envelope: CostEnvelope | None = None
    ollama_host: NonEmptyString = "http://127.0.0.1:11434"

    @model_validator(mode="after")
    def validate_enabled_editorial(self) -> EditorialConfig:
        if not self.enabled:
            return self
        if (
            self.provider != "ollama"
            or self.remote_processing
            or self.model_pair is None
            or self.cost_envelope is None
            or "article_summary" not in self.capabilities
            or self.model_pair.editorial_model == self.model_pair.verifier_model
        ):
            raise ValueError("enabled local editorial configuration is incomplete")
        return self


class Section(StrictModel):
    id: NonEmptyString
    title: NonEmptyString
    policy: NonEmptyString | None = None
    budget: Budget | None = None
    sources: list[NonEmptyString] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)


class Publication(StrictModel):
    id: NonEmptyString
    title: NonEmptyString
    language: NonEmptyString = "en"
    policies: dict[NonEmptyString, PolicyPreset] = Field(default_factory=dict)
    budget: Budget | None = None
    editorial: EditorialConfig | None = None
    sections: list[Section]


class Configuration(StrictModel):
    version: Literal[1]
    sources: dict[NonEmptyString, Source]
    publications: list[Publication]
    remote_providers: dict[NonEmptyString, RemoteProviderProfile] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_references_and_identifiers(self) -> Configuration:
        publication_ids: set[str] = set()
        source_ids = set(self.sources)
        provider_ids = set(self.remote_providers)

        for publication in self.publications:
            if publication.id in publication_ids:
                raise ValueError("duplicate publication id")
            publication_ids.add(publication.id)

            policy_ids = set(publication.policies)
            for policy in publication.policies.values():
                if not set(policy.source_weights).issubset(source_ids):
                    raise ValueError("policy references an unknown source")
                for mute_rule in policy.mute_rules:
                    if mute_rule.source is not None and mute_rule.source not in source_ids:
                        raise ValueError("mute rule references an unknown source")

            if publication.editorial is not None:
                provider = publication.editorial.provider
                if provider is not None and provider != "ollama" and provider not in provider_ids:
                    raise ValueError("editorial configuration references an unknown provider")

            section_ids: set[str] = set()
            self._validate_sections(publication.sections, section_ids, source_ids, policy_ids)

        return self

    @classmethod
    def _validate_sections(
        cls,
        sections: list[Section],
        section_ids: set[str],
        source_ids: set[str],
        policy_ids: set[str],
    ) -> None:
        for section in sections:
            if section.id in section_ids:
                raise ValueError("duplicate section id")
            section_ids.add(section.id)

            if section.policy is not None and section.policy not in policy_ids:
                raise ValueError("section references an unknown policy")
            if len(section.sources) != len(set(section.sources)):
                raise ValueError("section contains duplicate source references")
            if not set(section.sources).issubset(source_ids):
                raise ValueError("section references an unknown source")
            if section.sections and section.sources:
                raise ValueError("sources attach only to leaf sections")

            cls._validate_sections(section.sections, section_ids, source_ids, policy_ids)
