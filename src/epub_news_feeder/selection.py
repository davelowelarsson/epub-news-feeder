from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Policy(StrEnum):
    COVERAGE = "coverage"
    INTEREST = "interest"


@dataclass(frozen=True, slots=True)
class Candidate:
    article_id: str
    source_id: str
    title: str
    canonical_url: str
    published_at: datetime
    source_weight: int = 5


@dataclass(frozen=True, slots=True)
class SectionCandidate:
    article: Candidate
    relevance: int = 0
    interest_score: int = 0
    essential: bool = False
    muted: bool = False


@dataclass(frozen=True, slots=True)
class SectionRequest:
    section_id: str
    title: str
    order: int
    policy: Policy
    max_articles: int
    min_articles: int = 0
    weight: int = 5
    discovery_percent: float = 0.2
    candidates: tuple[SectionCandidate, ...] = ()


@dataclass(frozen=True, slots=True)
class PublicationRequest:
    max_articles: int
    min_articles: int
    sections: tuple[SectionRequest, ...]


@dataclass(frozen=True, slots=True)
class SelectedSlot:
    section_id: str
    section_title: str
    section_order: int
    rank: int
    article: Candidate
    relevance: int
    essential: bool
    interest_score: int


@dataclass(frozen=True, slots=True)
class SelectionResult:
    slots: tuple[SelectedSlot, ...]
    unique_article_ids: tuple[str, ...]
    meets_minimum: bool
    partial: bool
    warnings: tuple[str, ...]

    def for_section(self, section_id: str) -> tuple[SelectedSlot, ...]:
        return tuple(slot for slot in self.slots if slot.section_id == section_id)


@dataclass(frozen=True, slots=True)
class ArticlePlacement:
    article: Candidate
    primary_section_id: str
    pointer_section_ids: tuple[str, ...]
    relevant_section_ids: tuple[str, ...]


def _freshness(candidate: SectionCandidate) -> float:
    return candidate.article.published_at.timestamp()


def _apply_plurality(
    ordered: list[SectionCandidate], limit: int
) -> tuple[list[SectionCandidate], bool]:
    sources = {candidate.article.source_id for candidate in ordered}
    if len(sources) < 2 or limit < 2:
        return ordered[:limit], False
    cap = max(1, math.floor(limit * 0.6))
    selected: list[SectionCandidate] = []
    deferred: list[SectionCandidate] = []
    counts: dict[str, int] = {}
    for candidate in ordered:
        source = candidate.article.source_id
        if counts.get(source, 0) >= cap:
            deferred.append(candidate)
            continue
        selected.append(candidate)
        counts[source] = counts.get(source, 0) + 1
        if len(selected) == limit:
            return selected, False
    relaxed = bool(deferred and len(selected) < limit)
    selected.extend(deferred[: limit - len(selected)])
    return selected, relaxed


def _coverage_order(candidates: list[SectionCandidate]) -> list[SectionCandidate]:
    essential = sorted(
        (candidate for candidate in candidates if candidate.essential),
        key=lambda item: (-item.article.source_weight, -_freshness(item), item.article.source_id),
    )
    remaining = [candidate for candidate in candidates if not candidate.essential]
    by_source: dict[str, list[SectionCandidate]] = {}
    for candidate in remaining:
        by_source.setdefault(candidate.article.source_id, []).append(candidate)
    for source_candidates in by_source.values():
        source_candidates.sort(key=lambda item: (-_freshness(item), item.article.canonical_url))
    counts: dict[str, int] = {}
    for candidate in essential:
        source = candidate.article.source_id
        counts[source] = counts.get(source, 0) + 1
    ordered = list(essential)
    while any(by_source.values()):
        available = [source for source, values in by_source.items() if values]
        source = min(
            available,
            key=lambda value: (
                counts.get(value, 0) / max(1, by_source[value][0].article.source_weight),
                -_freshness(by_source[value][0]),
                value,
            ),
        )
        ordered.append(by_source[source].pop(0))
        counts[source] = counts.get(source, 0) + 1
    return ordered


def _interest_order(
    candidates: list[SectionCandidate], limit: int, discovery_percent: float
) -> list[SectionCandidate]:
    if not candidates:
        return []
    discovery_count = 0
    if limit >= 2:
        discovery_count = max(1, math.ceil(limit * discovery_percent))
    discovery = sorted(
        candidates,
        key=lambda item: (-_freshness(item), item.article.source_id, item.article.canonical_url),
    )[:discovery_count]
    discovery_ids = {candidate.article.article_id for candidate in discovery}
    influenced = sorted(
        (
            candidate
            for candidate in candidates
            if candidate.article.article_id not in discovery_ids
        ),
        key=lambda item: (
            -item.interest_score,
            -item.article.source_weight,
            -_freshness(item),
            item.article.source_id,
            item.article.canonical_url,
        ),
    )
    return influenced[: max(0, limit - len(discovery))] + discovery


def _rank_section(section: SectionRequest) -> tuple[list[SectionCandidate], bool]:
    eligible = [candidate for candidate in section.candidates if not candidate.muted]
    if section.policy == Policy.INTEREST:
        ordered = _interest_order(eligible, section.max_articles, section.discovery_percent)
    else:
        ordered = _coverage_order(eligible)
    return _apply_plurality(ordered, section.max_articles)


def select_publication(request: PublicationRequest) -> SelectionResult:
    ranked: dict[str, list[SectionCandidate]] = {}
    warnings: list[str] = []
    for section in sorted(request.sections, key=lambda item: item.order):
        ranked[section.section_id], relaxed = _rank_section(section)
        if relaxed:
            warnings.append(f"SOURCE_PLURALITY_RELAXED:{section.section_id}")

    positions = {section.section_id: 0 for section in request.sections}
    section_counts = {section.section_id: 0 for section in request.sections}
    selected: list[SelectedSlot] = []
    unique: list[str] = []
    unique_set: set[str] = set()
    ordered_sections = sorted(request.sections, key=lambda item: item.order)
    weighted_turns = [section for section in ordered_sections for _ in range(section.weight)]

    made_progress = True
    while made_progress and weighted_turns:
        made_progress = False
        for section in weighted_turns:
            if section_counts[section.section_id] >= section.max_articles:
                continue
            candidates = ranked[section.section_id]
            position = positions[section.section_id]
            while position < len(candidates):
                candidate = candidates[position]
                position += 1
                positions[section.section_id] = position
                is_new = candidate.article.article_id not in unique_set
                if is_new and len(unique) >= request.max_articles:
                    continue
                selected.append(
                    SelectedSlot(
                        section.section_id,
                        section.title,
                        section.order,
                        section_counts[section.section_id] + 1,
                        candidate.article,
                        candidate.relevance,
                        candidate.essential,
                        candidate.interest_score,
                    )
                )
                section_counts[section.section_id] += 1
                if is_new:
                    unique_set.add(candidate.article.article_id)
                    unique.append(candidate.article.article_id)
                made_progress = True
                break
    meets_minimum = len(unique) >= request.min_articles
    return SelectionResult(
        tuple(selected),
        tuple(unique),
        meets_minimum,
        len(unique) < request.max_articles,
        tuple(warnings),
    )


def place_articles(result: SelectionResult) -> dict[str, ArticlePlacement]:
    grouped: dict[str, list[SelectedSlot]] = {}
    for slot in result.slots:
        grouped.setdefault(slot.article.article_id, []).append(slot)
    placements: dict[str, ArticlePlacement] = {}
    for article_id, slots in grouped.items():
        primary = min(slots, key=lambda slot: (-slot.relevance, slot.section_order))
        secondary = sorted(
            (slot for slot in slots if slot.section_id != primary.section_id),
            key=lambda slot: (slot.section_order, slot.rank),
        )
        pointers = tuple(slot.section_id for slot in secondary)
        placements[article_id] = ArticlePlacement(
            primary.article,
            primary.section_id,
            pointers,
            (primary.section_id, *pointers),
        )
    return placements
