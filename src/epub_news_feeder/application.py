"""Synchronous local Edition generation pipeline."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from epub_news_feeder.acquisition import (
    AcquisitionMode,
    EligibilityEvidence,
    SourceClient,
    SourceRequest,
)
from epub_news_feeder.delivery import DeliveryReceipt, deliver_local
from epub_news_feeder.diagnostics import Diagnostics
from epub_news_feeder.epub import (
    ArticleInput,
    EditionInput,
    LinkInput,
    SectionInput,
    SectionPointerInput,
    build_epub,
)
from epub_news_feeder.models import (
    Budget,
    Configuration,
    MatchRule,
    PolicyPreset,
    Publication,
    Section,
)
from epub_news_feeder.selection import (
    Candidate,
    Policy,
    PublicationRequest,
    SectionCandidate,
    SectionRequest,
    place_articles,
    select_publication,
)
from epub_news_feeder.state import ArticleObservation, StateStore
from epub_news_feeder.validation import EpubValidationError, validate_epub


class GenerationError(Exception):
    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message
        super().__init__(safe_message)


@dataclass(frozen=True, slots=True)
class GenerationResult:
    receipt: DeliveryReceipt
    article_count: int
    partial: bool


@dataclass(frozen=True, slots=True)
class _ArticleRecord:
    article: ArticleInput
    observation: ArticleObservation
    categories: tuple[str, ...]
    published_at: datetime
    source_id: str


@dataclass(frozen=True, slots=True)
class _Leaf:
    section: Section
    policy_id: str | None
    budget: Budget | None


def generate_edition(
    configuration: Configuration,
    *,
    state_path: Path,
    output_directory: Path,
    diagnostics_directory: Path,
    run_id: str,
    generated_at: datetime,
    publication_id: str | None = None,
    epubcheck_jar: Path | None = None,
) -> GenerationResult:
    publication = _publication(configuration, publication_id)
    edition_id = f"{publication.id}@{generated_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    diagnostics = Diagnostics(diagnostics_directory, run_id)
    diagnostics.emit("RUN_STARTED", phase="run", publication_id=publication.id)

    with StateStore(state_path, environment="local") as state:
        try:
            state.begin_run(run_id, publication.id, edition_id, generated_at)
        except Exception as error:
            raise GenerationError(
                "RUN_STATE_FAILED", "Run state could not be initialized"
            ) from error
        try:
            return _run(
                configuration,
                publication,
                state,
                diagnostics,
                run_id,
                edition_id,
                generated_at,
                output_directory,
                epubcheck_jar,
            )
        except GenerationError as error:
            state.abandon_run(run_id, reason=error.code)
            diagnostics.emit(error.code, phase="run", outcome="failed")
            raise
        except Exception as error:
            state.abandon_run(run_id, reason="GENERATION_FAILED")
            diagnostics.emit("GENERATION_FAILED", phase="run", outcome="failed")
            raise GenerationError("GENERATION_FAILED", "Edition generation failed") from error


def _run(
    configuration: Configuration,
    publication: Publication,
    state: StateStore,
    diagnostics: Diagnostics,
    run_id: str,
    edition_id: str,
    generated_at: datetime,
    output_directory: Path,
    epubcheck_jar: Path | None,
) -> GenerationResult:
    leaves = tuple(_leaves(publication.sections))
    source_ids = tuple(dict.fromkeys(source for leaf in leaves for source in leaf.section.sources))
    records: dict[str, _ArticleRecord] = {}
    source_records: dict[str, list[str]] = {source_id: [] for source_id in source_ids}
    source_links: dict[str, list[LinkInput]] = {source_id: [] for source_id in source_ids}
    notes: list[str] = []

    client = SourceClient(now=lambda: generated_at)
    try:
        for source_id in source_ids:
            source = configuration.sources[source_id]
            if source.rights is None or source.eligibility is None:
                code = "SOURCE_RIGHTS_EVIDENCE_MISSING"
                state.record_source_health(
                    source_id, attempted_at=generated_at, succeeded=False, classification=code
                )
                diagnostics.emit(code, phase="acquisition", source_id=source_id)
                notes.append(f"{source.title} was omitted because eligibility evidence is missing.")
                continue
            evidence = source.eligibility
            outcome = client.acquire(
                SourceRequest(
                    source_id=source_id,
                    publisher_id=source.publisher_id or source_id,
                    title=source.title,
                    feed_url=str(source.feed_url),
                    mode=AcquisitionMode(source.acquisition),
                    llm_processing=source.llm_processing,
                    evidence=EligibilityEvidence(
                        evidence_id=evidence.evidence_id,
                        reviewed_at=datetime.combine(
                            evidence.evidence_reviewed_at, time.min, tzinfo=UTC
                        ),
                        expires_at=datetime.combine(
                            evidence.review_expires_at, time.max, tzinfo=UTC
                        ),
                        feed_acquisition=evidence.feed_acquisition,
                        page_acquisition=evidence.page_acquisition,
                        retention=evidence.retention,
                        private_distribution=evidence.private_distribution,
                        local_llm=evidence.local_llm,
                        remote_llm=evidence.remote_llm,
                    ),
                )
            )
            succeeded = outcome.code in {"SOURCE_OK", "SOURCE_PARTIAL"}
            state.record_source_health(
                source_id,
                attempted_at=generated_at,
                succeeded=succeeded,
                classification=outcome.code,
            )
            diagnostics.emit(
                outcome.code,
                phase="acquisition",
                source_id=source_id,
                articles=len(outcome.articles),
                omitted=outcome.omitted,
            )
            if outcome.code != "SOURCE_OK":
                notes.append(f"{source.title} was partially available or omitted ({outcome.code}).")
            for acquired in outcome.articles:
                if acquired.body is None:
                    source_links[source_id].append(
                        LinkInput(acquired.title, acquired.source_title, acquired.canonical_url)
                    )
                    continue
                observation = state.observe_article(
                    source_id=source_id,
                    publisher_id=acquired.publisher_id,
                    canonical_url=acquired.canonical_url,
                    guid=acquired.guid,
                    title=acquired.title,
                    author=acquired.author,
                    normalized_body=acquired.body,
                    observed_at=generated_at,
                    publication_id=publication.id,
                )
                if not observation.eligible:
                    continue
                record = _ArticleRecord(
                    article=ArticleInput(
                        identifier=observation.article_id,
                        title=acquired.title,
                        body=acquired.body,
                        source_name=acquired.source_title,
                        canonical_url=acquired.canonical_url,
                        author=acquired.author,
                    ),
                    observation=observation,
                    categories=acquired.categories,
                    published_at=acquired.published_at or generated_at,
                    source_id=source_id,
                )
                records[observation.article_id] = record
                source_records[source_id].append(observation.article_id)
    finally:
        client.close()

    request = _selection_request(publication, leaves, configuration, records, source_records)
    selection = select_publication(request)
    for warning in selection.warnings:
        diagnostics.emit(warning, phase="selection")
    if not selection.meets_minimum:
        raise GenerationError(
            "PUBLICATION_BELOW_MINIMUM", "Eligible articles did not meet the publication minimum"
        )

    placements = place_articles(selection)
    sections: list[SectionInput] = []
    for leaf in leaves:
        section_id = leaf.section.id
        primary = tuple(
            records[article_id].article
            for article_id, placement in placements.items()
            if placement.primary_section_id == section_id
        )
        pointers = tuple(
            SectionPointerInput(
                article_identifier=article_id,
                headline=records[article_id].article.title,
                source_name=records[article_id].article.source_name,
                relevance_reason=f"Also relevant to {leaf.section.title}",
            )
            for article_id, placement in placements.items()
            if section_id in placement.pointer_section_ids
        )
        links = tuple(
            link for source_id in leaf.section.sources for link in source_links[source_id]
        )
        sections.append(SectionInput(section_id, leaf.section.title, primary, pointers, links))

    edition = EditionInput(
        title=publication.title,
        identifier=edition_id,
        language=publication.language,
        run_id=run_id,
        sections=tuple(sections),
        notes=tuple(notes),
        modified_at=generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    epub_bytes = build_epub(edition)
    try:
        validate_epub(epub_bytes, jar_path=epubcheck_jar)
    except EpubValidationError as error:
        raise GenerationError("EPUB_VALIDATION_FAILED", str(error)) from error
    diagnostics.emit("EPUB_VALID", phase="validation", articles=len(selection.unique_article_ids))

    observations = [records[article_id].observation for article_id in selection.unique_article_ids]
    state.reserve_articles(
        run_id,
        publication.id,
        observations,
        expires_at=generated_at + timedelta(hours=24),
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    filename = f"epub-news--{generated_at.strftime('%Y-%m-%dT%H%M%SZ')}--{run_id}.epub"
    try:
        receipt = deliver_local(
            epub_bytes, output_directory=output_directory, filename=filename
        )
    except (OSError, ValueError) as error:
        raise GenerationError("LOCAL_DELIVERY_FAILED", "Local Delivery failed") from error
    state.finalize_delivery(
        run_id, publication.id, delivered_at=generated_at, delivery_digest=receipt.sha256
    )
    diagnostics.emit(
        "EDITION_DELIVERED",
        phase="delivery",
        articles=len(selection.unique_article_ids),
        partial=selection.partial,
        digest=receipt.sha256,
    )
    return GenerationResult(receipt, len(selection.unique_article_ids), selection.partial)


def _publication(configuration: Configuration, publication_id: str | None) -> Publication:
    if publication_id is None:
        if not configuration.publications:
            raise GenerationError("PUBLICATION_NOT_FOUND", "No publication is configured")
        return configuration.publications[0]
    for publication in configuration.publications:
        if publication.id == publication_id:
            return publication
    raise GenerationError("PUBLICATION_NOT_FOUND", "Requested publication is not configured")


def _leaves(
    sections: list[Section], policy_id: str | None = None, budget: Budget | None = None
) -> list[_Leaf]:
    leaves: list[_Leaf] = []
    for section in sections:
        inherited_policy = section.policy or policy_id
        inherited_budget = section.budget or budget
        if section.sections:
            leaves.extend(_leaves(section.sections, inherited_policy, inherited_budget))
        else:
            leaves.append(_Leaf(section, inherited_policy, inherited_budget))
    return leaves


def _selection_request(
    publication: Publication,
    leaves: tuple[_Leaf, ...],
    configuration: Configuration,
    records: dict[str, _ArticleRecord],
    source_records: dict[str, list[str]],
) -> PublicationRequest:
    default_max = min(18, max(6, 3 * len(leaves)))
    maximum = publication.budget.max_articles if publication.budget else None
    minimum = publication.budget.min_articles if publication.budget else None
    pub_max = maximum or default_max
    pub_min = minimum if minimum is not None else max(1, math.ceil(pub_max * 0.25))
    requests: list[SectionRequest] = []
    for order, leaf in enumerate(leaves):
        policy = publication.policies.get(leaf.policy_id) if leaf.policy_id else None
        policy = policy or PolicyPreset(type="coverage")
        budget = leaf.budget
        section_max = budget.max_articles if budget and budget.max_articles else pub_max
        section_min = budget.min_articles if budget and budget.min_articles is not None else 0
        section_weight = budget.weight if budget and budget.weight else 5
        candidates: list[SectionCandidate] = []
        for source_id in leaf.section.sources:
            for article_id in source_records[source_id]:
                record = records[article_id]
                source = configuration.sources[source_id]
                candidate = Candidate(
                    article_id,
                    source_id,
                    record.article.title,
                    record.article.canonical_url,
                    record.published_at,
                    policy.source_weights.get(source_id, source.weight),
                )
                candidates.append(
                    SectionCandidate(
                        candidate,
                        relevance=_score(policy.positive_rules, record),
                        interest_score=_score(policy.positive_rules, record)
                        - _score(policy.negative_rules, record),
                        essential=any(_matches(rule, record) for rule in policy.essential_coverage),
                        muted=any(
                            (mute.source == source_id)
                            or (mute.rule is not None and _matches(mute.rule, record))
                            for mute in policy.mute_rules
                        ),
                    )
                )
        requests.append(
            SectionRequest(
                leaf.section.id,
                leaf.section.title,
                order,
                Policy(policy.type),
                section_max,
                section_min,
                section_weight,
                policy.discovery_slice,
                tuple(candidates),
            )
        )
    return PublicationRequest(pub_max, pub_min, tuple(requests))


def _score(rules: list[MatchRule], record: _ArticleRecord) -> int:
    return sum((rule.weight or 1) for rule in rules if _matches(rule, record))


def _matches(rule: MatchRule, record: _ArticleRecord) -> bool:
    values: tuple[str, ...]
    if rule.field == "title":
        values = (record.article.title,)
    elif rule.field == "category":
        values = record.categories
    else:
        values = (record.article.body,)
    needle = rule.value.casefold()
    if rule.match == "phrase":
        return any(needle in value.casefold() for value in values)
    pattern = re.compile(rf"(?<!\w){re.escape(needle)}(?!\w)", re.IGNORECASE)
    return any(pattern.search(value) is not None for value in values)
