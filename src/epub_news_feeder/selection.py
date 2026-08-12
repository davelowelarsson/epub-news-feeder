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
    cluster_id: str | None = None


@dataclass(frozen=True, slots=True)
class SectionCandidate:
    article: Candidate
    relevance: int = 0
    interest_score: int = 0
    essential: bool = False
    muted: bool = False
    # On how many distinct days a referenced Publication delivered into this Article's Story
    # Cluster. Zero for every Publication that reads no other's history, which is why it can
    # enter the ordering unconditionally without changing any existing Edition.
    recurrence: int = 0


@dataclass(frozen=True, slots=True)
class AncestorBudget:
    """A named ancestor ceiling shared by one or more leaf Sections.

    The ceiling counts Article identities once across the ancestor subtree, while
    individual Sections count every Canonical Rendition or Section Pointer slot.
    """

    ancestor_id: str
    max_articles: int


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
    minimum_sources: int = 2
    single_source_cap: float = 0.6
    ancestor_budgets: tuple[AncestorBudget, ...] = ()


@dataclass(frozen=True, slots=True)
class BriefCandidate:
    """One Publisher Link Brief competing only against other Briefs.

    A Brief carries no relevance, weight or feedback: it is a headline and a route, and
    ranking machinery built for an Article would be disproportionate to two seconds of reading.
    """

    brief_id: str
    source_id: str
    title: str
    canonical_url: str
    published_at: datetime
    muted: bool = False


@dataclass(frozen=True, slots=True)
class PublicationRequest:
    max_articles: int
    min_articles: int
    sections: tuple[SectionRequest, ...]
    max_briefs: int = 0
    briefs: tuple[BriefCandidate, ...] = ()


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
    selected_briefs: tuple[BriefCandidate, ...] = ()

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
    ordered: list[SectionCandidate],
    limit: int,
    minimum_sources: int,
    single_source_cap: float,
) -> tuple[list[SectionCandidate], bool]:
    sources = {candidate.article.source_id for candidate in ordered}
    if len(sources) < minimum_sources or limit < minimum_sources:
        return ordered[:limit], False
    cap = max(1, math.floor(limit * single_source_cap))
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
    # The Essential Coverage Slice is ordered by the policy alone, recurrence deliberately
    # excluded. Essentials bypass the cluster diversification below - `essentials + diversified`
    # - so letting recurrence sort them would emit one story from three publishers back to back
    # at the head of the Section with nothing left to break it up. Essential means the Edition
    # owes the reader this, which is not a claim recurrence gets to reorder.
    essential = sorted(
        (candidate for candidate in candidates if candidate.essential),
        key=lambda item: (-item.article.source_weight, -_freshness(item), item.article.source_id),
    )
    remaining = [candidate for candidate in candidates if not candidate.essential]
    by_source: dict[str, list[SectionCandidate]] = {}
    for candidate in remaining:
        by_source.setdefault(candidate.article.source_id, []).append(candidate)
    # Within a Source, the week's continuing thread comes before the merely fresher item.
    for source_candidates in by_source.values():
        source_candidates.sort(
            key=lambda item: (-item.recurrence, -_freshness(item), item.article.canonical_url)
        )
    counts: dict[str, int] = {}
    for candidate in essential:
        source = candidate.article.source_id
        counts[source] = counts.get(source, 0) + 1
    ordered = list(essential)
    while any(by_source.values()):
        available = [source for source, values in by_source.items() if values]
        # Plurality still decides which Source is due; recurrence only breaks the tie between
        # Sources that are equally due, ahead of freshness. Putting it before the count would
        # let one Source's continuing story defeat the plurality the Section exists to have.
        source = min(
            available,
            key=lambda value: (
                counts.get(value, 0) / max(1, by_source[value][0].article.source_weight),
                -by_source[value][0].recurrence,
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
    if discovery_percent > 0 and limit > 0:
        discovery_count = max(1, math.ceil(limit * discovery_percent))
    unscored = [candidate for candidate in candidates if candidate.interest_score == 0]
    discovery = sorted(
        unscored or candidates,
        key=lambda item: (-_freshness(item), item.article.source_id, item.article.canonical_url),
    )[:discovery_count]
    discovery_ids = {candidate.article.article_id for candidate in discovery}
    # Recurrence ranks below the reader's declared interest and above Source weight. Below,
    # because an interest Section is where somebody said what they want and the week's most
    # covered story does not overrule that; above, because weight is a proxy the reader never
    # stated. Where a policy declares no rules - as `personlig` does not - every interest_score
    # is 0 and recurrence becomes the leading discriminator, which is the intent.
    #
    # It must be here rather than applied afterwards: this sort is followed immediately by a
    # truncation to `limit`, so a promotion layered on the result could only reorder Articles
    # already chosen and could never pull a recurring thread into the Section.
    influenced = sorted(
        (
            candidate
            for candidate in candidates
            if candidate.article.article_id not in discovery_ids
        ),
        key=lambda item: (
            -item.interest_score,
            -item.recurrence,
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
    # A stable partition, so each group keeps the order its policy chose - including the
    # recurrence ranking, which both policies applied above rather than here. Recurrence used to
    # be a promotion at this line, which was wrong twice over: the interest path had already
    # truncated to `max_articles`, so it could only reorder Articles rather than promote one into
    # the Section, and it re-sorted the essential block, which never reaches the diversification
    # below. Cluster diversification then keeps a recurrent story leading without letting it
    # monopolise: the first pick is the most recurrent, the next is from another cluster.
    ordered.sort(key=lambda candidate: not candidate.essential)
    essentials = [candidate for candidate in ordered if candidate.essential]
    remaining = [candidate for candidate in ordered if not candidate.essential]
    cluster_counts: dict[str, int] = {}
    diversified: list[SectionCandidate] = []
    while remaining:
        index = min(
            range(len(remaining)),
            key=lambda position: cluster_counts.get(
                remaining[position].article.cluster_id
                or f"unclustered:{remaining[position].article.article_id}",
                0,
            ),
        )
        candidate = remaining.pop(index)
        cluster = candidate.article.cluster_id or f"unclustered:{candidate.article.article_id}"
        cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
        diversified.append(candidate)
    ordered = essentials + diversified
    return _apply_plurality(
        ordered,
        section.max_articles,
        section.minimum_sources,
        section.single_source_cap,
    )


def _normalized_minima(
    sections: list[SectionRequest], publication_maximum: int
) -> tuple[dict[str, int], bool]:
    requested = {
        section.section_id: min(section.min_articles, section.max_articles) for section in sections
    }
    total = sum(requested.values())
    if total <= publication_maximum:
        return requested, False
    if publication_maximum == 0:
        return {section.section_id: 0 for section in sections}, True
    shares = {
        section.section_id: requested[section.section_id] * publication_maximum / total
        for section in sections
    }
    allocated = {section_id: math.floor(share) for section_id, share in shares.items()}
    remaining = publication_maximum - sum(allocated.values())
    for section in sorted(
        sections,
        key=lambda item: (-(shares[item.section_id] - allocated[item.section_id]), item.order),
    )[:remaining]:
        allocated[section.section_id] += 1
    return allocated, True


def _ancestor_maxima(sections: list[SectionRequest]) -> dict[str, int]:
    maxima: dict[str, int] = {}
    for section in sections:
        for budget in section.ancestor_budgets:
            existing = maxima.get(budget.ancestor_id)
            if existing is not None and existing != budget.max_articles:
                raise ValueError("Ancestor Budget must use one maximum everywhere in its subtree")
            maxima[budget.ancestor_id] = budget.max_articles
    return maxima


def _select_briefs(request: PublicationRequest) -> tuple[BriefCandidate, ...]:
    """Fill the Brief roll round-robin across Sources, then present it newest first.

    Selection order and presentation order are deliberately different. Round-robin selection
    stops one Source filling the roll; chronological presentation stops the chapter
    re-fragmenting into per-publisher lists.
    """

    by_source: dict[str, list[BriefCandidate]] = {}
    for brief in request.briefs:
        if brief.muted:
            continue
        by_source.setdefault(brief.source_id, []).append(brief)
    for candidates in by_source.values():
        candidates.sort(key=lambda item: (-item.published_at.timestamp(), item.canonical_url))

    taken: list[BriefCandidate] = []
    while len(taken) < request.max_briefs and any(by_source.values()):
        for source_id in sorted(by_source):
            if len(taken) >= request.max_briefs:
                break
            if by_source[source_id]:
                taken.append(by_source[source_id].pop(0))
    return tuple(
        sorted(
            taken,
            key=lambda item: (-item.published_at.timestamp(), item.source_id, item.canonical_url),
        )
    )


def select_publication(request: PublicationRequest) -> SelectionResult:
    ranked: dict[str, list[SectionCandidate]] = {}
    warnings: list[str] = []
    for section in sorted(request.sections, key=lambda item: item.order):
        ranked[section.section_id], relaxed = _rank_section(section)
        if relaxed:
            warnings.append(f"SOURCE_PLURALITY_RELAXED:{section.section_id}")

    ordered_sections = sorted(request.sections, key=lambda item: item.order)
    positions = {section.section_id: 0 for section in ordered_sections}
    section_counts = {section.section_id: 0 for section in request.sections}
    section_article_ids = {section.section_id: set[str]() for section in request.sections}
    selected: list[SelectedSlot] = []
    unique: list[str] = []
    unique_set: set[str] = set()
    ancestor_maxima = _ancestor_maxima(ordered_sections)
    ancestor_article_ids = {ancestor_id: set[str]() for ancestor_id in ancestor_maxima}

    def can_add(section: SectionRequest, candidate: SectionCandidate) -> bool:
        article_id = candidate.article.article_id
        if article_id in section_article_ids[section.section_id]:
            return False
        is_new = article_id not in unique_set
        if is_new and len(unique) >= request.max_articles:
            return False
        for budget in section.ancestor_budgets:
            article_ids = ancestor_article_ids[budget.ancestor_id]
            if article_id not in article_ids and len(article_ids) >= budget.max_articles:
                return False
        return True

    def add_next(section: SectionRequest, *, essential_only: bool = False) -> bool:
        if section_counts[section.section_id] >= section.max_articles:
            return False
        candidates = ranked[section.section_id]
        position = positions[section.section_id]
        while position < len(candidates):
            candidate = candidates[position]
            if essential_only and not candidate.essential:
                return False
            position += 1
            positions[section.section_id] = position
            if not can_add(section, candidate):
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
            section_article_ids[section.section_id].add(candidate.article.article_id)
            if candidate.article.article_id not in unique_set:
                unique_set.add(candidate.article.article_id)
                unique.append(candidate.article.article_id)
            for budget in section.ancestor_budgets:
                ancestor_article_ids[budget.ancestor_id].add(candidate.article.article_id)
            return True
        return False

    made_progress = True
    while made_progress:
        made_progress = False
        for section in ordered_sections:
            made_progress = add_next(section, essential_only=True) or made_progress

    minima, normalized = _normalized_minima(ordered_sections, request.max_articles)
    if normalized:
        warnings.append("SECTION_MINIMA_NORMALIZED")
    made_progress = True
    while made_progress:
        made_progress = False
        for section in ordered_sections:
            if section_counts[section.section_id] < minima[section.section_id]:
                made_progress = add_next(section) or made_progress

    weighted_turns = [section for section in ordered_sections for _ in range(section.weight)]

    made_progress = True
    while made_progress and weighted_turns:
        made_progress = False
        for section in weighted_turns:
            made_progress = add_next(section) or made_progress
    # Briefs are counted apart from Articles everywhere: they never enter the unique Article
    # identity set, and an Edition of pure headlines is a notification rather than a reading
    # product, so they cannot satisfy the Publication minimum either.
    meets_minimum = len(unique) >= request.min_articles
    return SelectionResult(
        tuple(selected),
        tuple(unique),
        meets_minimum,
        len(unique) < request.max_articles,
        tuple(warnings),
        _select_briefs(request),
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
