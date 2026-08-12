from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, model_validator

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
LanguageTag = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$",
    ),
]
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
    default_article_language: LanguageTag | None = None
    publisher_id: NonEmptyString | None = None
    copyright_notice: NonEmptyString | None = None
    allowed_publisher_origins: list[HttpUrl] = Field(default_factory=list)
    feed_url: HttpUrl
    acquisition: Literal["auto", "feed", "web", "metadata_only"] = "auto"
    presentation: Literal["full_text", "briefing_roll"] | None = None
    weight: Weight = 5
    llm_processing: Literal["disabled", "local_only", "remote_allowed"] = "local_only"
    rights: RightsPolicy | None = None
    eligibility: EligibilityEvidence | None = None

    @property
    def effective_presentation(self) -> Literal["full_text", "briefing_roll"]:
        """Derive presentation from acquisition unless an operator overrode it.

        Operators configure the least they can get away with, so the default has to be right
        without being written down: a Source permitted only metadata produces Briefs.
        """

        if self.presentation is not None:
            return self.presentation
        return "briefing_roll" if self.acquisition == "metadata_only" else "full_text"


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
            self.provider is None
            or self.model_pair is None
            or self.cost_envelope is None
            or "article_summary" not in self.capabilities
            or self.model_pair.editorial_model == self.model_pair.verifier_model
        ):
            raise ValueError("enabled editorial configuration is incomplete")
        # `remote_processing` is not an independent switch: it states which route the named
        # provider is, and a configuration that disagrees with itself about whether Article
        # text leaves the machine is the one thing that must never load.
        if self.remote_processing != (self.provider != "ollama"):
            raise ValueError("editorial remote_processing contradicts the configured provider")
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
    language: LanguageTag = "en"
    policies: dict[NonEmptyString, PolicyPreset] = Field(default_factory=dict)
    budget: Budget | None = None
    # Briefs are capped entirely outside the Article Budget: a Brief never consumes an
    # Article Slot, so it has no business inside a structure scoped to Article content.
    max_briefs: NonNegativeInt = 6
    # Named Publications whose delivery history this one may read. Suppression is
    # per-Publication by design, and that boundary is load-bearing, so widening it is opt-in,
    # explicit and one-directional: the weekly may know what the daily delivered, while the
    # daily stays unaware the weekly exists. A Publication that names nothing here behaves
    # exactly as it did before this field existed.
    reads_history_from: list[NonEmptyString] = Field(default_factory=list)
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
                if provider is not None and provider in provider_ids:
                    _require_private_provider_profile(self.remote_providers[provider])

            section_ids: set[str] = set()
            self._validate_sections(publication.sections, section_ids, source_ids, policy_ids)

        # A second pass, because a history reference may name a Publication declared later in
        # the file. Order in a configuration file is presentation, not dependency.
        for publication in self.publications:
            referenced_ids: set[str] = set()
            for referenced in publication.reads_history_from:
                if referenced == publication.id:
                    raise ValueError("publication reads its own delivery history")
                if referenced not in publication_ids:
                    raise ValueError("publication reads an unknown publication's history")
                if referenced in referenced_ids:
                    raise ValueError("duplicate publication history reference")
                referenced_ids.add(referenced)
        self._reject_history_cycles()

        return self

    def _reject_history_cycles(self) -> None:
        """History references are one-directional, so a cycle is a configuration error.

        Two Publications each suppressing against the other has no defensible reading: whichever
        ran first would silently decide what the other could carry. Rejecting it here is cheaper
        than explaining the resulting Edition.
        """

        references = {
            publication.id: tuple(publication.reads_history_from)
            for publication in self.publications
        }
        visiting: set[str] = set()
        settled: set[str] = set()

        def visit(publication_id: str) -> None:
            if publication_id in settled:
                return
            if publication_id in visiting:
                raise ValueError("publication history references form a cycle")
            visiting.add(publication_id)
            for referenced in references.get(publication_id, ()):
                visit(referenced)
            visiting.discard(publication_id)
            settled.add(publication_id)

        for publication_id in references:
            visit(publication_id)

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


def _require_private_provider_profile(profile: RemoteProviderProfile) -> None:
    """Refuse to load a configuration whose remote provider profile is not private enough.

    This is the earliest possible gate: a profile declaring training opt-in or server-side
    storage fails at load, before a Source is fetched, rather than at the moment Article
    text would already be in flight. `tools` is typed to `none` and needs no check here.
    """

    if profile.training_opt_in:
        raise ValueError("a remote editorial provider must not opt in to training")
    if profile.store:
        raise ValueError("a remote editorial provider must not store responses")
