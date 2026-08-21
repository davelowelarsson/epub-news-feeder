"""Synchronous local Edition generation pipeline."""

from __future__ import annotations

import math
import re
import sqlite3
import unicodedata
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path

from epub_news_feeder.acquisition import (
    AcquisitionMode,
    EligibilityEvidence,
    SourceClient,
    SourceRequest,
)
from epub_news_feeder.delivery import DeliveryReceipt, deliver_local
from epub_news_feeder.diagnostics import Diagnostics
from epub_news_feeder.drive import (
    DriveAuthError,
    DriveClient,
    DriveError,
    archive_due_editions,
    deliver_drive,
)
from epub_news_feeder.editorial import (
    ArticleEvidence,
    StructuredProvider,
    StructuredProviderError,
    generate_editorial,
)
from epub_news_feeder.epub import (
    ArticleInput,
    BodyBlock,
    BriefInput,
    CorrectionInput,
    EditionInput,
    EditorialCitationInput,
    EditorialSentenceInput,
    EditorialSummaryInput,
    NavigationInput,
    PriorCoverageInput,
    SectionInput,
    SectionPointerInput,
    StoryArticleLinkInput,
    StoryHubInput,
    build_epub,
)
from epub_news_feeder.models import (
    Configuration,
    EditorialConfig,
    MatchRule,
    PolicyPreset,
    Publication,
    Section,
)
from epub_news_feeder.ollama import OllamaStructuredProvider
from epub_news_feeder.openai_responses import OpenAIResponsesProvider
from epub_news_feeder.selection import (
    AncestorBudget,
    BriefCandidate,
    Candidate,
    Policy,
    PublicationRequest,
    SectionCandidate,
    SectionRequest,
    place_articles,
    select_publication,
)
from epub_news_feeder.state import (
    ArticleObservation,
    PendingBrief,
    StateStore,
    normalize_text,
)
from epub_news_feeder.state import brief_id as durable_brief_id
from epub_news_feeder.state_sync import (
    StateSyncAuthError,
    StateSyncError,
    restore_state,
    save_state,
)
from epub_news_feeder.validation import EpubValidationError, validate_epub

# How far back a referenced Publication's Story Clusters still count as recurring. Seven days
# because the Publication this exists for is weekly: a thread the daily returned to across the
# week is the thing a Saturday Edition should lead with, while one it dropped in March is not
# recurring, it is over. Cluster coverage is never pruned, so without a window the archive would
# eventually outrank the week.
_RECURRENCE_WINDOW = timedelta(days=7)

# How long a Delivery Copy stays in the Drive delivery folder before housekeeping moves it
# to the archive folder. Seven days keeps roughly one week visible on the device — five
# dailies and a weekly — which is what the folder is for; everything older is history and
# belongs in the archive, retrievable but not in the way.
_DELIVERY_RETENTION_DAYS = 7

# Publication Notes are reader-facing end matter, so they speak the Publication Language —
# an English apology inside a Swedish Edition reads as a defect, not a notice.
_NOTE_TEMPLATES: dict[str, dict[str, str]] = {
    "en": {
        "evidence_missing": "{title} was omitted because eligibility evidence is missing.",
        "unavailable": "Some reporting from {title} was unavailable for this Edition.",
    },
    "sv": {
        "evidence_missing": "{title} utelämnades eftersom rättighetsunderlag saknas.",
        "unavailable": "Viss rapportering från {title} var inte tillgänglig i den här utgåvan.",
    },
}


def _note(language: str, key: str, *, title: str) -> str:
    table = _NOTE_TEMPLATES.get(language.split("-", 1)[0].casefold(), _NOTE_TEMPLATES["en"])
    return table[key].format(title=title)


class GenerationError(Exception):
    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message
        super().__init__(safe_message)


class RetryableGenerationError(GenerationError):
    """A validated immutable Edition remains pending and must not be abandoned."""


@dataclass(frozen=True, slots=True)
class DriveTarget:
    """An opt-in additional Delivery Target; local delivery always happens first."""

    client: DriveClient
    folder_id: str
    # Where expired Delivery Copies are moved after a successful delivery. None disables
    # housekeeping entirely, which is what every caller did before the folder existed.
    archive_folder_id: str | None = None


@dataclass(frozen=True, slots=True)
class StateSyncTarget:
    """Opt-in scheduled State Store persistence via Drive, surviving an empty runner disk."""

    client: DriveClient
    folder_id: str
    environment: str = "local"


@dataclass(frozen=True, slots=True)
class GenerationResult:
    receipt: DeliveryReceipt
    article_count: int
    partial: bool
    brief_count: int = 0

    @property
    def read_item_count(self) -> int:
        return self.article_count + self.brief_count


@dataclass(frozen=True, slots=True)
class _ArticleRecord:
    article: ArticleInput
    observation: ArticleObservation
    categories: tuple[str, ...]
    published_at: datetime
    publisher_published_at: datetime | None
    source_id: str
    publisher_id: str
    cluster_id: str | None


@dataclass(frozen=True, slots=True)
class _BriefRecord:
    """A Publisher Link Brief awaiting selection into its own capped roll."""

    brief: BriefInput
    categories: tuple[str, ...]
    published_at: datetime
    source_id: str


type _SelectableRecord = _ArticleRecord
type _MatchableRecord = _ArticleRecord | _BriefRecord


@dataclass(frozen=True, slots=True)
class _Leaf:
    section: Section
    policy_id: str | None
    ancestor_budgets: tuple[AncestorBudget, ...]


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
    drive_target: DriveTarget | None = None,
    state_sync_target: StateSyncTarget | None = None,
) -> GenerationResult:
    publication = _publication(configuration, publication_id)
    edition_id = f"{publication.id}@{generated_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    diagnostics = Diagnostics(diagnostics_directory, run_id)
    diagnostics.emit("RUN_STARTED", phase="run", publication_id=publication.id)

    if state_sync_target is not None:
        _restore_state(state_sync_target, state_path, diagnostics)

    with StateStore(state_path, environment="local") as state:
        try:
            state.begin_run(run_id, publication.id, edition_id, generated_at)
        except Exception as error:
            raise GenerationError(
                "RUN_STATE_FAILED", "Run state could not be initialized"
            ) from error
        try:
            result = _run(
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
            if drive_target is not None:
                _deliver_to_drive(result, drive_target, diagnostics)
                _archive_delivered_editions(drive_target, generated_at, diagnostics)
            if state_sync_target is not None:
                _save_state(state, state_sync_target, diagnostics)
            return result
        except RetryableGenerationError as error:
            with suppress(OSError):
                diagnostics.emit(error.code, phase="run", outcome="pending")
            raise
        except GenerationError as error:
            state.abandon_run(run_id, reason=error.code)
            with suppress(OSError):
                diagnostics.emit(error.code, phase="run", outcome="failed")
            raise
        except Exception as error:
            with suppress(Exception):
                state.abandon_run(run_id, reason="GENERATION_FAILED")
            with suppress(OSError):
                diagnostics.emit("GENERATION_FAILED", phase="run", outcome="failed")
            raise GenerationError("GENERATION_FAILED", "Edition generation failed") from error


def _restore_state(
    state_sync_target: StateSyncTarget, state_path: Path, diagnostics: Diagnostics
) -> None:
    """Restore the scheduled State Store from Drive before it is ever opened, fail-closed.

    A verified archive, or a legitimate first run (a clean absence), both proceed. Anything
    else — a download failure, a digest mismatch, or an ambiguous existence check — aborts:
    proceeding with an empty store would silently re-deliver everything already delivered.

    A rejected credential aborts too, but under its own code. The two failures need entirely
    different responses — one is renewed, the other investigated — and a single code for both
    sends the operator looking at Drive when the answer is an expired refresh token.
    """

    try:
        restore_state(
            client=state_sync_target.client,
            folder_id=state_sync_target.folder_id,
            state_path=state_path,
            environment=state_sync_target.environment,
            diagnostics=diagnostics,
        )
    except StateSyncAuthError as error:
        with suppress(OSError):
            diagnostics.emit("DRIVE_AUTH_FAILED", phase="state", outcome="failed")
        raise GenerationError("DRIVE_AUTH_FAILED", str(error)) from error
    except StateSyncError as error:
        with suppress(OSError):
            diagnostics.emit("STATE_RESTORE_FAILED", phase="state", outcome="failed")
        raise GenerationError(
            "STATE_RESTORE_FAILED", "Scheduled State Store restore could not be verified"
        ) from error


def _save_state(
    state: StateStore, state_sync_target: StateSyncTarget, diagnostics: Diagnostics
) -> None:
    """Save the State Store to Drive only after the Edition has been delivered and finalized.

    A save failure must be loud rather than silent: it raises RetryableGenerationError so the
    already-finalized Run is not reported as a clean success, without abandoning it — the next
    attempt can retry the save without re-delivering the Edition.
    """

    try:
        save_state(
            client=state_sync_target.client,
            folder_id=state_sync_target.folder_id,
            state_path=state.path,
            environment=state_sync_target.environment,
            connection=state.connection,
            diagnostics=diagnostics,
        )
    except StateSyncAuthError as error:
        # Retryable, not abandoned: the Edition is already delivered and finalized. The code
        # still names the credential, because that is what the next attempt needs fixed first.
        raise RetryableGenerationError("DRIVE_AUTH_FAILED", str(error)) from error
    except StateSyncError as error:
        raise RetryableGenerationError(
            "STATE_SAVE_PENDING", "Scheduled State Store save to Drive remains pending"
        ) from error


def _deliver_to_drive(
    result: GenerationResult, drive_target: DriveTarget, diagnostics: Diagnostics
) -> None:
    """Deliver the already-locally-delivered Edition to Drive as an additional target.

    Local delivery has already completed by the time this runs. A Drive failure must not
    abandon the Run: it raises RetryableGenerationError so the validated, already-delivered
    Edition remains available for a retry to reconcile, mirroring the local-delivery-pending
    path.
    """

    try:
        receipt = deliver_drive(
            result.receipt.path.read_bytes(),
            client=drive_target.client,
            folder_id=drive_target.folder_id,
            filename=result.receipt.path.name,
        )
    except DriveAuthError as error:
        raise RetryableGenerationError(
            "DRIVE_AUTH_FAILED",
            "Google rejected the Drive refresh token; renew it with "
            "`epub-news-feeder authorize-drive`",
        ) from error
    except (DriveError, OSError, ValueError) as error:
        raise RetryableGenerationError(
            "DRIVE_DELIVERY_PENDING", "Validated Drive Delivery remains pending"
        ) from error
    with suppress(OSError):
        diagnostics.emit("DRIVE_DELIVERED", phase="delivery", digest=receipt.sha256)


def _archive_delivered_editions(
    drive_target: DriveTarget, generated_at: datetime, diagnostics: Diagnostics
) -> None:
    """Move expired Delivery Copies into the archive folder, fail-soft.

    Housekeeping runs only after the Edition is delivered, and a housekeeping failure must
    never fail the run that just succeeded: the Edition is on the device either way, and a
    crowded folder is a smaller problem than a red morning run. The failure stays visible
    as a diagnostic rather than an exit code, and the next morning's run tries again.
    """

    if drive_target.archive_folder_id is None:
        return
    try:
        archived = archive_due_editions(
            client=drive_target.client,
            delivery_folder_id=drive_target.folder_id,
            archive_folder_id=drive_target.archive_folder_id,
            generated_at_date=generated_at.astimezone(UTC).date(),
            retention_days=_DELIVERY_RETENTION_DAYS,
        )
    except DriveError:
        with suppress(OSError):
            diagnostics.emit("DRIVE_ARCHIVE_FAILED", phase="delivery", outcome="pending")
        return
    if archived:
        with suppress(OSError):
            diagnostics.emit("DRIVE_ARCHIVED", phase="delivery", archived=len(archived))


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
    resumed = _resume_spooled_delivery(
        state,
        diagnostics,
        publication,
        run_id,
        generated_at,
        output_directory,
        epubcheck_jar,
    )
    if resumed is not None:
        return resumed
    source_ids = tuple(dict.fromkeys(source for leaf in leaves for source in leaf.section.sources))
    records: dict[str, _SelectableRecord] = {}
    briefs: dict[str, _BriefRecord] = {}
    source_records: dict[str, list[str]] = {source_id: [] for source_id in source_ids}
    notes: list[str] = []
    degraded_source_ids: set[str] = set()

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
                notes.append(
                    _note(publication.language, "evidence_missing", title=source.title)
                )
                degraded_source_ids.add(source_id)
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
                    default_article_language=source.default_article_language,
                    allowed_publisher_origins=tuple(
                        str(origin) for origin in source.allowed_publisher_origins
                    ),
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
                evidence_id=evidence.evidence_id,
                articles=len(outcome.articles),
                omitted=outcome.omitted,
            )
            if outcome.code != "SOURCE_OK":
                degraded_source_ids.add(source_id)
                notes.append(_note(publication.language, "unavailable", title=source.title))
            for acquired in outcome.articles:
                if acquired.body is None:
                    # A body-free item only ever reaches here from a metadata_only Source, so a
                    # Brief stays a rights outcome and never becomes a failure outcome. Identity
                    # is the durable, body-free brief_id: it is also the in-run element id, so
                    # there is one identity rather than two.
                    brief_id = durable_brief_id(acquired.canonical_url)
                    briefs[brief_id] = _BriefRecord(
                        brief=BriefInput(
                            identifier=brief_id,
                            title=acquired.title,
                            source_name=acquired.source_title,
                            canonical_url=acquired.canonical_url,
                            published_at=(
                                acquired.published_at.astimezone(UTC).date().isoformat()
                                if acquired.published_at is not None
                                else None
                            ),
                            language=acquired.language,
                        ),
                        categories=acquired.categories,
                        published_at=acquired.published_at or generated_at,
                        source_id=source_id,
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
                    run_id=run_id,
                )
                if not observation.eligible:
                    continue
                article_record = _ArticleRecord(
                    article=ArticleInput(
                        identifier=observation.article_id,
                        title=acquired.title,
                        body=acquired.body,
                        blocks=tuple(
                            BodyBlock(kind=block.kind, text=block.text) for block in acquired.blocks
                        ),
                        source_name=acquired.source_title,
                        canonical_url=acquired.canonical_url,
                        language=acquired.language,
                        author=acquired.author,
                        published_at=(
                            acquired.published_at.astimezone(UTC).date().isoformat()
                            if acquired.published_at is not None
                            else None
                        ),
                        materially_updated=observation.materially_changed,
                        copyright_notice=source.copyright_notice,
                    ),
                    observation=observation,
                    categories=acquired.categories,
                    published_at=acquired.published_at or generated_at,
                    publisher_published_at=acquired.published_at,
                    source_id=source_id,
                    publisher_id=acquired.publisher_id,
                    cluster_id=state.match_story_cluster(
                        observation.article_id,
                        signals=_story_signals(acquired.title, acquired.categories),
                        observed_at=generated_at,
                    ),
                )
                records.setdefault(observation.article_id, article_record)
                source_records[source_id].append(observation.article_id)
    finally:
        client.close()

    _suppress_title_twins(records, source_records, diagnostics)

    # A Brief already delivered is suppressed at candidacy, before it ever reaches selection: a
    # reworded headline on the same report is not new. Every Publication this one reads is asked
    # too, not only this one - `brief_id` is derived from the canonical URL, so it is the same
    # identity in every Publication, and a weekly whose Articles were deduplicated while its
    # Brief roll reprinted the week would have deduplicated the cheaper half of the Edition.
    delivered_brief_ids: set[str] = set()
    for history_id in (publication.id, *publication.reads_history_from):
        delivered_brief_ids |= state.delivered_brief_ids(history_id)
    briefs = {
        brief_id: record
        for brief_id, record in briefs.items()
        if brief_id not in delivered_brief_ids
    }

    # The same suppression across Publications, for a Publication that names one. Deliberately
    # coarser than this Publication's own revision-aware history: there, a materially revised
    # Article may legitimately come back. Here the question is not whether the text moved but
    # whether the reader already read the story, and a Saturday Edition reprinting Wednesday's
    # report with three sentences changed has failed at being a Saturday Edition.
    #
    # An empty or absent referenced history suppresses nothing and the Edition goes out anyway.
    # Nothing was delivered there, so nothing is owed suppression - correct rather than degraded,
    # and withholding Saturday because the daily is younger than a week would punish a fresh
    # install. Either way it reports `omitted`, so a Saturday Edition that quietly stopped
    # deduplicating is visible in the log rather than only in the reading.
    recurrence: dict[str, int] = {}
    if publication.reads_history_from:
        read_elsewhere: set[str] = set()
        for referenced_id in publication.reads_history_from:
            delivered_there = state.delivered_article_ids((referenced_id,))
            read_elsewhere |= delivered_there
            diagnostics.emit(
                "HISTORY_SUPPRESSED",
                phase="selection",
                publication_id=referenced_id,
                omitted=sum(1 for article_id in records if article_id in delivered_there),
            )
        for candidate_ids in source_records.values():
            candidate_ids[:] = [
                article_id for article_id in candidate_ids if article_id not in read_elsewhere
            ]
        recurrence = state.cluster_recurrence(
            publication.reads_history_from, since=generated_at - _RECURRENCE_WINDOW
        )
        diagnostics.emit("HISTORY_RECURRENCE", phase="selection", clusters=len(recurrence))

    request = _selection_request(
        publication,
        leaves,
        configuration,
        records,
        source_records,
        briefs,
        generated_at,
        recurrence,
    )
    selection = select_publication(request)
    for warning in selection.warnings:
        diagnostics.emit(warning, phase="selection")
    if not selection.meets_minimum:
        raise GenerationError(
            "PUBLICATION_BELOW_MINIMUM", "Eligible articles did not meet the publication minimum"
        )
    for article_id in selection.unique_article_ids:
        record = records[article_id]
        diagnostics.emit(
            "ARTICLE_SELECTED",
            phase="selection",
            article_id=article_id,
            source_id=record.source_id,
        )
    for selected_brief in selection.selected_briefs:
        diagnostics.emit(
            "BRIEF_SELECTED",
            phase="selection",
            source_id=selected_brief.source_id,
        )

    placements = place_articles(selection)
    _apply_editorial(
        publication,
        configuration,
        records,
        selection.unique_article_ids,
        generated_at,
        diagnostics,
    )
    cluster_articles: dict[str, list[str]] = {}
    for article_id in selection.unique_article_ids:
        record = records[article_id]
        cluster_id = record.cluster_id
        if cluster_id is not None:
            cluster_articles.setdefault(cluster_id, []).append(article_id)
    hubs_by_section: dict[str, list[StoryHubInput]] = {}
    for cluster_id, article_ids in cluster_articles.items():
        # Every Publication whose history this one reads, not only this one. A weekly promotes a
        # thread precisely because the daily kept returning to it, so asking only itself returns
        # nothing - it has never delivered into that Cluster - and the `< 2 and not prior` guard
        # below would then drop the Story Hub from the one Article that most needs the timeline.
        seen_prior: set[str] = set()
        prior = []
        for history_id in (publication.id, *publication.reads_history_from):
            for coverage in state.prior_cluster_coverage(history_id, cluster_id, limit=10):
                if coverage.article_id in article_ids or coverage.article_id in seen_prior:
                    continue
                seen_prior.add(coverage.article_id)
                prior.append(coverage)
        prior.sort(key=lambda coverage: (coverage.delivered_at, coverage.article_id), reverse=True)
        prior = prior[:3]
        if len(article_ids) < 2 and not prior:
            continue
        host_section = placements[article_ids[0]].primary_section_id
        hubs_by_section.setdefault(host_section, []).append(
            StoryHubInput(
                cluster_id,
                tuple(
                    StoryArticleLinkInput(
                        article_id,
                        _record_title(records[article_id]),
                        _record_source_name(records[article_id]),
                    )
                    for article_id in article_ids
                ),
                tuple(
                    PriorCoverageInput(
                        coverage.title,
                        _publisher_title(configuration, coverage.publisher_id),
                        coverage.canonical_url,
                        coverage.publisher_published_at.astimezone(UTC).date().isoformat(),
                    )
                    for coverage in prior
                ),
            )
        )
    sections: list[SectionInput] = []
    for leaf in leaves:
        section_id = leaf.section.id
        primary_items: list[ArticleInput] = []
        for article_id, placement in placements.items():
            if placement.primary_section_id != section_id:
                continue
            primary_items.append(records[article_id].article)
        primary = tuple(primary_items)
        pointers = tuple(
            SectionPointerInput(
                article_identifier=article_id,
                headline=_record_title(records[article_id]),
                source_name=_record_source_name(records[article_id]),
            )
            for article_id, placement in placements.items()
            if section_id in placement.pointer_section_ids
        )
        sections.append(
            SectionInput(
                identifier=section_id,
                title=leaf.section.title,
                articles=primary,
                pointers=pointers,
                has_edition_note=any(
                    source_id in degraded_source_ids for source_id in leaf.section.sources
                ),
                story_hubs=tuple(hubs_by_section.get(section_id, ())),
            )
        )

    edition = EditionInput(
        title=publication.title,
        identifier=edition_id,
        language=publication.language,
        run_id=run_id,
        sections=tuple(sections),
        navigation=_navigation(publication.sections),
        notes=tuple(notes),
        corrections=tuple(
            CorrectionInput(
                correction.title,
                _publisher_title(configuration, correction.publisher_id),
                correction.canonical_url,
                correction.kind,
                correction.signaled_at.astimezone(UTC).date().isoformat(),
            )
            for correction in state.pending_corrections(publication.id)
        ),
        briefs=tuple(briefs[item.brief_id].brief for item in selection.selected_briefs),
        editorial_excluded_sources=_editorial_excluded_sources(
            publication, configuration, records, selection.unique_article_ids, generated_at
        ),
        editorial_route=(
            "remote"
            if publication.editorial is not None and publication.editorial.remote_processing
            else "local"
        ),
        edition_date=generated_at.astimezone(UTC).date().isoformat(),
        modified_at=generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    epub_bytes = build_epub(edition)
    try:
        validate_epub(epub_bytes, jar_path=epubcheck_jar)
    except EpubValidationError as error:
        raise GenerationError("EPUB_VALIDATION_FAILED", str(error)) from error
    selected_articles = [records[article_id] for article_id in selection.unique_article_ids]
    selected_briefs = selection.selected_briefs
    diagnostics.emit(
        "EPUB_VALID",
        phase="validation",
        articles=len(selected_articles),
        briefs=len(selected_briefs),
        read_items=len(selection.unique_article_ids) + len(selected_briefs),
    )
    observations = [record.observation for record in selected_articles]
    state.reserve_articles(
        run_id,
        publication.id,
        observations,
        expires_at=generated_at + timedelta(hours=24),
        article_count=len(selected_articles),
        publisher_link_count=len(selected_briefs),
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    output_directory.chmod(0o700)
    filename = edition_filename(
        publication_id=publication.id, generated_at=generated_at, run_id=run_id
    )
    spool_directory = state.path.parent / "pending-editions"
    spool_directory.mkdir(parents=True, exist_ok=True)
    spool_directory.chmod(0o700)
    spool_receipt = deliver_local(
        epub_bytes, output_directory=spool_directory, filename=f"{run_id}.epub"
    )
    state.prepare_delivery(
        run_id=run_id,
        publication_id=publication.id,
        delivery_target=str(output_directory / filename),
        delivery_digest=spool_receipt.sha256,
        prepared_at=generated_at,
        briefs=[
            PendingBrief(
                brief_id=selected_brief.brief_id,
                source_id=selected_brief.source_id,
                published_at=selected_brief.published_at,
            )
            for selected_brief in selected_briefs
        ],
    )
    try:
        receipt = deliver_local(
            spool_receipt.path.read_bytes(),
            output_directory=output_directory,
            filename=filename,
        )
    except (OSError, ValueError) as error:
        raise RetryableGenerationError(
            "LOCAL_DELIVERY_PENDING", "Validated local Delivery remains pending"
        ) from error
    try:
        state.finalize_delivery(
            run_id, publication.id, delivered_at=generated_at, delivery_digest=receipt.sha256
        )
    except (OSError, RuntimeError) as error:
        raise RetryableGenerationError(
            "DELIVERY_FINALIZATION_PENDING", "Delivered copy awaits State finalization"
        ) from error
    with suppress(sqlite3.Error):
        state.acknowledge_corrections(
            publication.id,
            (correction.signal_id for correction in state.pending_corrections(publication.id)),
            delivered_at=generated_at,
        )
        for article_id in selection.unique_article_ids:
            record = records[article_id]
            if (
                not isinstance(record, _ArticleRecord)
                or record.cluster_id is None
                or record.publisher_published_at is None
            ):
                continue
            state.record_cluster_delivery(
                publication_id=publication.id,
                cluster_id=record.cluster_id,
                article_id=article_id,
                title=record.article.title,
                publisher_id=record.publisher_id,
                canonical_url=record.article.canonical_url,
                publisher_published_at=record.publisher_published_at,
                delivered_at=generated_at,
            )
        for selected_brief in selected_briefs:
            state.record_brief_delivery(
                publication_id=publication.id,
                brief_id=selected_brief.brief_id,
                source_id=selected_brief.source_id,
                published_at=selected_brief.published_at,
                delivered_at=generated_at,
                run_id=run_id,
            )
    with suppress(OSError):
        diagnostics.emit(
            "EDITION_DELIVERED",
            phase="delivery",
            articles=len(selected_articles),
            briefs=len(selected_briefs),
            read_items=len(selection.unique_article_ids) + len(selected_briefs),
            partial=selection.partial,
            digest=receipt.sha256,
        )
    spool_receipt.path.unlink(missing_ok=True)
    return GenerationResult(
        receipt,
        len(selected_articles),
        selection.partial,
        brief_count=len(selected_briefs),
    )


_MAXIMUM_SLUG_LENGTH = 20


def edition_filename(*, publication_id: str, generated_at: datetime, run_id: str) -> str:
    """Name a Delivery Copy date-first, so a reader sees the date before the name is cut.

    A Kobo shows a filename truncated from the right, and the Drive folder is read by eye:
    the date is the one part that must always survive, the Publication next, and the Run's
    random suffix last — it exists only to keep two Editions of one day from colliding.
    """

    slug = _slugify(publication_id)[:_MAXIMUM_SLUG_LENGTH].strip("-") or "edition"
    return f"{generated_at.strftime('%Y-%m-%d')}-{slug}-{run_id.rsplit('-', 1)[-1]}.epub"


def _slugify(value: str) -> str:
    """Fold to ASCII (so ``väder`` becomes ``vader``) and keep only filename-safe runs."""

    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", folded.casefold()).strip("-")


def _resume_spooled_delivery(
    state: StateStore,
    diagnostics: Diagnostics,
    publication: Publication,
    run_id: str,
    generated_at: datetime,
    output_directory: Path,
    epubcheck_jar: Path | None,
) -> GenerationResult | None:
    pending = state.pending_deliveries(publication.id)
    current = next((delivery for delivery in pending if delivery.run_id == run_id), None)
    spool_path = state.path.parent / "pending-editions" / f"{run_id}.epub"
    filename = edition_filename(
        publication_id=publication.id, generated_at=generated_at, run_id=run_id
    )
    target = output_directory / filename
    status, delivered_digest = state.run_delivery_status(run_id)
    if status == "delivered":
        if delivered_digest is None or not target.is_file():
            raise GenerationError(
                "DELIVERED_COPY_MISSING", "Delivered State cannot reconcile its local copy"
            )
        receipt = deliver_local(
            target.read_bytes(), output_directory=output_directory, filename=filename
        )
        if receipt.sha256 != delivered_digest:
            raise GenerationError(
                "DELIVERED_COPY_MISMATCH", "Delivered local copy is not immutable"
            )
        spool_path.unlink(missing_ok=True)
        article_count, publisher_link_count = state.run_item_counts(run_id)
        _, publication_maximum = _publication_limits(publication)
        return GenerationResult(
            receipt,
            article_count,
            article_count + publisher_link_count < publication_maximum,
            brief_count=publisher_link_count,
        )
    if current is None and not spool_path.is_file():
        if pending:
            raise GenerationError(
                "EARLIER_DELIVERY_PENDING", "An earlier validated Delivery must be resumed"
            )
        if status == "validated":
            raise GenerationError(
                "VALIDATED_ARTIFACT_MISSING",
                "Validated reservations have no immutable Delivery artifact",
            )
        return None
    if current is not None and Path(current.delivery_target) != target:
        raise GenerationError("DELIVERY_TARGET_MISMATCH", "Delivery Target is immutable")
    if not spool_path.is_file() and current is not None and target.is_file():
        target_bytes = target.read_bytes()
        if sha256(target_bytes).hexdigest() == current.delivery_digest:
            receipt = deliver_local(
                target_bytes, output_directory=output_directory, filename=filename
            )
            state.finalize_delivery(
                run_id,
                publication.id,
                delivered_at=generated_at,
                delivery_digest=receipt.sha256,
            )
            with suppress(sqlite3.Error):
                for pending_brief in current.briefs:
                    state.record_brief_delivery(
                        publication_id=publication.id,
                        brief_id=pending_brief.brief_id,
                        source_id=pending_brief.source_id,
                        published_at=pending_brief.published_at,
                        delivered_at=generated_at,
                        run_id=run_id,
                    )
            article_count, publisher_link_count = state.run_item_counts(run_id)
            _, publication_maximum = _publication_limits(publication)
            return GenerationResult(
                receipt,
                article_count,
                article_count + publisher_link_count < publication_maximum,
                brief_count=publisher_link_count,
            )
    if not spool_path.is_file():
        raise GenerationError(
            "DELIVERY_SPOOL_MISSING", "Validated Delivery artifact is unavailable"
        )
    epub_bytes = spool_path.read_bytes()
    digest = sha256(epub_bytes).hexdigest()
    try:
        validate_epub(epub_bytes, jar_path=epubcheck_jar)
    except EpubValidationError as error:
        raise RetryableGenerationError(
            "DELIVERY_SPOOL_INVALID", "Validated Delivery spool failed revalidation"
        ) from error
    if current is not None and (
        current.delivery_digest != digest or Path(current.delivery_target) != target
    ):
        raise RetryableGenerationError(
            "DELIVERY_SPOOL_MISMATCH", "Validated Delivery spool is immutable"
        )
    if current is None:
        # The prior attempt crashed before its own Brief selection could be staged: nothing
        # durable records which Briefs it chose, so none are recorded here either.
        current = state.prepare_delivery(
            run_id=run_id,
            publication_id=publication.id,
            delivery_target=str(target),
            delivery_digest=digest,
            prepared_at=generated_at,
        )
    output_directory.mkdir(parents=True, exist_ok=True)
    output_directory.chmod(0o700)
    try:
        receipt = deliver_local(epub_bytes, output_directory=output_directory, filename=filename)
        state.finalize_delivery(
            run_id, publication.id, delivered_at=generated_at, delivery_digest=digest
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise RetryableGenerationError(
            "DELIVERY_FINALIZATION_PENDING", "Delivered copy awaits State finalization"
        ) from error
    with suppress(sqlite3.Error):
        for pending_brief in current.briefs:
            state.record_brief_delivery(
                publication_id=publication.id,
                brief_id=pending_brief.brief_id,
                source_id=pending_brief.source_id,
                published_at=pending_brief.published_at,
                delivered_at=generated_at,
                run_id=run_id,
            )
    article_count, publisher_link_count = state.run_item_counts(run_id)
    _, publication_maximum = _publication_limits(publication)
    with suppress(OSError):
        diagnostics.emit(
            "EDITION_DELIVERED",
            phase="delivery",
            articles=article_count,
            briefs=publisher_link_count,
            read_items=article_count + publisher_link_count,
            partial=article_count + publisher_link_count < publication_maximum,
            digest=receipt.sha256,
        )
    spool_path.unlink(missing_ok=True)
    return GenerationResult(
        receipt,
        article_count,
        article_count + publisher_link_count < publication_maximum,
        brief_count=publisher_link_count,
    )


def _suppress_title_twins(
    records: dict[str, _SelectableRecord],
    source_records: dict[str, list[str]],
    diagnostics: Diagnostics,
) -> None:
    """One publisher carrying one headline at two URLs is one story, not two Articles.

    Observed live: a publisher re-published the same piece at a second URL under another
    byline, and both filled Article Slots in one Section of one Edition. Identity is per
    canonical URL, and the two bodies differed too much for near-duplicate merging, so this
    run's candidate pool is the last place left to notice. The fullest body wins; freshness
    and then identity break the remaining ties, so the survivor is deterministic. Scoped to
    one publisher deliberately — two publishers sharing a headline is Story Cluster
    territory, not an identity question.
    """

    by_story: dict[tuple[str, str], list[str]] = {}
    for article_id, record in records.items():
        story = (record.publisher_id, normalize_text(record.article.title).casefold())
        by_story.setdefault(story, []).append(article_id)
    for twins in by_story.values():
        if len(twins) < 2:
            continue
        kept = max(
            twins,
            key=lambda article_id: (
                len(records[article_id].article.body.split()),
                records[article_id].published_at,
                article_id,
            ),
        )
        for article_id in twins:
            if article_id == kept:
                continue
            record = records.pop(article_id)
            for candidate_ids in source_records.values():
                candidate_ids[:] = [
                    candidate_id for candidate_id in candidate_ids if candidate_id != article_id
                ]
            diagnostics.emit(
                "ARTICLE_TITLE_TWIN_SUPPRESSED",
                phase="selection",
                article_id=article_id,
                source_id=record.source_id,
            )


def _publication(configuration: Configuration, publication_id: str | None) -> Publication:
    if publication_id is None:
        if not configuration.publications:
            raise GenerationError("PUBLICATION_NOT_FOUND", "No publication is configured")
        return configuration.publications[0]
    for publication in configuration.publications:
        if publication.id == publication_id:
            return publication
    raise GenerationError("PUBLICATION_NOT_FOUND", "Requested publication is not configured")


def _publisher_title(configuration: Configuration, publisher_id: str) -> str:
    for source_id, source in configuration.sources.items():
        if (source.publisher_id or source_id) == publisher_id:
            return source.title
    return publisher_id


def _leaves(
    sections: list[Section],
    policy_id: str | None = None,
    ancestor_budgets: tuple[AncestorBudget, ...] = (),
) -> list[_Leaf]:
    leaves: list[_Leaf] = []
    for section in sections:
        inherited_policy = section.policy or policy_id
        if section.sections:
            child_ancestors = ancestor_budgets
            if section.budget is not None and section.budget.max_articles is not None:
                child_ancestors = (
                    *ancestor_budgets,
                    AncestorBudget(section.id, section.budget.max_articles),
                )
            leaves.extend(_leaves(section.sections, inherited_policy, child_ancestors))
        else:
            leaves.append(_Leaf(section, inherited_policy, ancestor_budgets))
    return leaves


def _navigation(sections: list[Section]) -> tuple[NavigationInput, ...]:
    return tuple(
        NavigationInput(section.id, section.title, _navigation(section.sections))
        for section in sections
    )


def _selection_request(
    publication: Publication,
    leaves: tuple[_Leaf, ...],
    configuration: Configuration,
    records: dict[str, _SelectableRecord],
    source_records: dict[str, list[str]],
    briefs: dict[str, _BriefRecord],
    generated_at: datetime,
    recurrence: dict[str, int] | None = None,
) -> PublicationRequest:
    pub_min, pub_max = _publication_limits(publication)
    requests = _section_requests(
        publication,
        leaves,
        configuration,
        records,
        source_records,
        generated_at,
        pub_max,
        recurrence or {},
    )
    return PublicationRequest(
        pub_max,
        pub_min,
        tuple(requests),
        publication.max_briefs,
        _brief_candidates(publication, leaves, briefs, generated_at),
    )


def _brief_candidates(
    publication: Publication,
    leaves: tuple[_Leaf, ...],
    briefs: dict[str, _BriefRecord],
    generated_at: datetime,
) -> tuple[BriefCandidate, ...]:
    """Offer every acquired Brief, muted where any Section carrying its Source mutes it.

    Mute Rules are the one selection input a Brief still respects: a muted topic arriving as a
    headline is exactly the failure a Mute Rule exists to prevent. Relevance, weight, essential
    coverage and feedback do not apply.
    """

    policies_by_source: dict[str, list[PolicyPreset]] = {}
    for leaf in leaves:
        policy = publication.policies.get(leaf.policy_id) if leaf.policy_id else None
        policy = policy or PolicyPreset(type="coverage")
        for source_id in leaf.section.sources:
            policies_by_source.setdefault(source_id, []).append(policy)
    return tuple(
        BriefCandidate(
            brief_id=brief_id,
            source_id=record.source_id,
            title=record.brief.title,
            canonical_url=record.brief.canonical_url,
            published_at=record.published_at,
            muted=any(
                _brief_muted(policy, record, generated_at)
                for policy in policies_by_source.get(record.source_id, [])
            ),
        )
        for brief_id, record in briefs.items()
    )


def _brief_muted(policy: PolicyPreset, record: _BriefRecord, generated_at: datetime) -> bool:
    for mute in policy.mute_rules:
        if mute.expires_at is not None and mute.expires_at < generated_at.date():
            continue
        if mute.source == record.source_id:
            return True
        if mute.rule is not None and mute.rule.field != "body" and _matches(mute.rule, record):
            return True
    return False


def _publication_limits(publication: Publication) -> tuple[int, int]:
    default_max = min(18, max(6, 3 * len(publication.sections)))
    maximum = publication.budget.max_articles if publication.budget else None
    minimum = publication.budget.min_articles if publication.budget else None
    pub_max = maximum or default_max
    pub_min = minimum if minimum is not None else max(1, math.ceil(pub_max * 0.25))
    return pub_min, pub_max


def _section_requests(
    publication: Publication,
    leaves: tuple[_Leaf, ...],
    configuration: Configuration,
    records: dict[str, _SelectableRecord],
    source_records: dict[str, list[str]],
    generated_at: datetime,
    pub_max: int,
    recurrence: dict[str, int],
) -> list[SectionRequest]:
    requests: list[SectionRequest] = []
    for order, leaf in enumerate(leaves):
        policy = publication.policies.get(leaf.policy_id) if leaf.policy_id else None
        policy = policy or PolicyPreset(type="coverage")
        budget = leaf.section.budget
        section_max = min(
            pub_max,
            budget.max_articles if budget and budget.max_articles else pub_max,
            *(ancestor.max_articles for ancestor in leaf.ancestor_budgets),
        )
        section_min = budget.min_articles if budget and budget.min_articles is not None else 0
        section_weight = budget.weight if budget and budget.weight else 5
        candidates: list[SectionCandidate] = []
        for source_id in leaf.section.sources:
            for article_id in source_records[source_id]:
                record = records[article_id]
                if _outside_age_window(record, policy, generated_at):
                    continue
                source = configuration.sources[source_id]
                candidate = Candidate(
                    article_id,
                    source_id,
                    _record_title(record),
                    _record_canonical_url(record),
                    record.published_at,
                    policy.source_weights.get(source_id, source.weight),
                    record.cluster_id,
                )
                candidates.append(
                    SectionCandidate(
                        candidate,
                        relevance=_score(policy.positive_rules, record, policy.full_body_matching),
                        interest_score=_score(
                            policy.positive_rules, record, policy.full_body_matching
                        )
                        - _score(policy.negative_rules, record, policy.full_body_matching),
                        essential=any(
                            _matches(rule, record)
                            for rule in policy.essential_coverage
                            if rule.field != "body" or policy.full_body_matching
                        ),
                        muted=any(
                            (mute.expires_at is None or mute.expires_at >= generated_at.date())
                            and (
                                (mute.source == source_id)
                                or (
                                    mute.rule is not None
                                    and (mute.rule.field != "body" or policy.full_body_matching)
                                    and _matches(mute.rule, record)
                                )
                            )
                            for mute in policy.mute_rules
                        ),
                        # Unclustered Articles score zero rather than being excluded: a story
                        # nothing else covered is not thereby less worth reading, it simply
                        # carries no recurrence signal to be led by.
                        recurrence=(
                            recurrence.get(record.cluster_id, 0) if record.cluster_id else 0
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
                policy.minimum_sources,
                policy.single_source_cap,
                leaf.ancestor_budgets,
            )
        )
    return requests


def _outside_age_window(
    record: _ArticleRecord, policy: PolicyPreset, generated_at: datetime
) -> bool:
    """Whether a candidate's publisher date falls outside the policy's age window.

    Observed live: a Section starved of fresh reporting reached a month into the feed and
    delivered a notice for an exhibition that had already closed. A Partial Edition is more
    honest than a stale one. An undated Article is treated as fresh — absence of a date is
    the publisher's omission, not evidence of age.
    """

    if policy.max_age_days is None or record.publisher_published_at is None:
        return False
    return record.publisher_published_at < generated_at - timedelta(days=policy.max_age_days)


def _score(rules: list[MatchRule], record: _MatchableRecord, full_body_matching: bool) -> int:
    return sum(
        (rule.weight or 1)
        for rule in rules
        if (rule.field != "body" or full_body_matching) and _matches(rule, record)
    )


def _matches(rule: MatchRule, record: _MatchableRecord) -> bool:
    values: tuple[str, ...]
    if rule.field == "title":
        values = (_record_title(record),)
    elif rule.field == "category":
        values = record.categories
    else:
        values = (record.article.body,) if isinstance(record, _ArticleRecord) else ()
    needle = rule.value.casefold()
    if rule.match == "phrase":
        return any(needle in value.casefold() for value in values)
    pattern = re.compile(rf"(?<!\w){re.escape(needle)}(?!\w)", re.IGNORECASE)
    return any(pattern.search(value) is not None for value in values)


def _record_title(record: _MatchableRecord) -> str:
    return record.article.title if isinstance(record, _ArticleRecord) else record.brief.title


def _record_source_name(record: _MatchableRecord) -> str:
    return (
        record.article.source_name
        if isinstance(record, _ArticleRecord)
        else record.brief.source_name
    )


def _record_canonical_url(record: _MatchableRecord) -> str:
    return (
        record.article.canonical_url
        if isinstance(record, _ArticleRecord)
        else record.brief.canonical_url
    )


def _apply_editorial(
    publication: Publication,
    configuration: Configuration,
    records: dict[str, _SelectableRecord],
    selected_ids: tuple[str, ...],
    generated_at: datetime,
    diagnostics: Diagnostics,
) -> None:
    editorial = publication.editorial
    if editorial is None or not editorial.enabled or editorial.model_pair is None:
        return
    remote = editorial.remote_processing
    diagnostics.emit(
        "EDITORIAL_ROUTE",
        phase="editorial",
        route="remote" if remote else "local",
        provider=editorial.provider or "unknown",
    )
    eligible_records = [
        record
        for article_id in selected_ids
        if isinstance((record := records[article_id]), _ArticleRecord)
        and _allows_editorial(
            configuration, record.source_id, remote=remote, generated_at=generated_at
        )
        and record.article.language is not None
    ]
    if not eligible_records:
        diagnostics.emit("EDITORIAL_OMITTED", phase="editorial", calls=0)
        return
    max_tokens = editorial.cost_envelope.max_tokens if editorial.cost_envelope else 12000
    max_calls = editorial.cost_envelope.max_calls if editorial.cost_envelope else 4
    body_character_budget = min(2000, max(1000, max_tokens * 4 // len(eligible_records)))
    evidence = tuple(
        ArticleEvidence(
            article_id=record.article.identifier,
            title=record.article.title,
            publisher=record.article.source_name,
            canonical_url=record.article.canonical_url,
            published_at=record.article.published_at or generated_at.date().isoformat(),
            language=record.article.language or "und",
            lead_passage=_lead_passage(record.article.body),
            body=record.article.body[:body_character_budget],
        )
        for record in eligible_records
    )
    try:
        provider = _editorial_provider(editorial)
    except StructuredProviderError:
        diagnostics.emit("EDITORIAL_OMITTED", phase="editorial", calls=0)
        return
    results = []
    reserved_calls = 0
    budget_omissions = 0
    for batch in _editorial_batches(evidence):
        if reserved_calls + 4 > max_calls:
            budget_omissions += 1
            continue
        reserved_calls += 4
        result = generate_editorial(batch, editorial.model_pair, provider)
        results.append(result)
        usage = provider.drain_usage()
        diagnostics.emit(
            "EDITORIAL_MEASURED",
            phase="editorial",
            articles=len(batch),
            calls=result.evidence.calls,
            duration_ms=sum(result.evidence.call_durations_ms),
            input_tokens=sum(item.input_tokens for item in usage),
            output_tokens=sum(item.output_tokens for item in usage),
            input_characters=sum(len(item.body) for item in batch),
        )
    additions = [addition for result in results for addition in result.additions]
    calls = sum(result.evidence.calls for result in results)
    failure_codes = [
        result.evidence.failure_code
        for result in results
        if result.evidence.failure_code is not None
    ]
    diagnostics.emit(
        "EDITORIAL_ACCEPTED" if additions else "EDITORIAL_OMITTED",
        phase="editorial",
        calls=calls,
        omitted=len(failure_codes) + budget_omissions,
        **(
            {"reason": failure_codes[0] if failure_codes else "call_budget"}
            if not additions and (failure_codes or budget_omissions)
            else {}
        ),
    )
    evidence_by_id = {item.article.identifier: item for item in eligible_records}
    for addition in additions:
        target = records.get(addition.article_id)
        if not isinstance(target, _ArticleRecord):
            continue
        sentences = tuple(
            EditorialSentenceInput(
                sentence.text,
                tuple(
                    EditorialCitationInput(
                        evidence_by_id[citation_id].article.source_name,
                        evidence_by_id[citation_id].article.canonical_url,
                    )
                    for citation_id in sentence.citations
                    if citation_id in evidence_by_id
                ),
            )
            for sentence in addition.sentences
        )
        if sentences and all(sentence.citations for sentence in sentences):
            records[addition.article_id] = replace(
                target,
                article=replace(
                    target.article,
                    editorial_summary=EditorialSummaryInput(sentences),
                ),
            )


def _editorial_batches(
    evidence: tuple[ArticleEvidence, ...],
) -> tuple[tuple[ArticleEvidence, ...], ...]:
    by_language: dict[str, list[ArticleEvidence]] = {}
    for item in evidence:
        language = item.language.casefold().split("-", 1)[0]
        by_language.setdefault(language, []).append(item)
    return tuple((item,) for language in sorted(by_language) for item in by_language[language])


def _editorial_excluded_sources(
    publication: Publication,
    configuration: Configuration,
    records: dict[str, _SelectableRecord],
    selected_ids: tuple[str, ...],
    generated_at: datetime,
) -> tuple[str, ...]:
    """Name the Sources in this Edition whose publishers do not permit generated summaries.

    A silently absent summary is indistinguishable from a failed one, so a deliberate policy
    outcome is stated once in end matter rather than marked on every Article.

    The disclosure follows the route this Edition actually took. A publisher who permits a
    local model but not a remote one is excluded from a remote Edition and named as such,
    which is the honest statement — not the one a local Edition would have made.
    """

    if publication.editorial is None or not publication.editorial.enabled:
        return ()
    remote = publication.editorial.remote_processing
    excluded = {
        records[article_id].source_id
        for article_id in selected_ids
        if not _allows_editorial(
            configuration, records[article_id].source_id, remote=remote, generated_at=generated_at
        )
    }
    return tuple(sorted(configuration.sources[source_id].title for source_id in excluded))


def _allows_editorial(
    configuration: Configuration,
    source_id: str,
    *,
    remote: bool,
    generated_at: datetime,
) -> bool:
    """Decide, per Source, whether this Edition's editorial route may read that Source.

    The two limbs are deliberately not one relaxed rule. Local processing asks only whether
    the operator's own machine may run a model over the text. Remote processing asks
    something categorically different — whether the text may leave the machine at all — so
    it requires the publisher's own recorded `remote_llm` allowance, an operator policy that
    says so in as many words, a rights basis, and evidence still inside its review window.

    Silence is never permission on the remote limb: `conditional` and `unknown` both refuse.
    """

    source = configuration.sources[source_id]
    if source.llm_processing == "disabled" or source.eligibility is None:
        return False
    if not remote:
        return source.eligibility.local_llm == "allow"
    # Acquisition already refuses an expired Source, so this can only fire as defence in
    # depth — which is exactly what the one route that leaves the machine should have.
    expires_at = datetime.combine(source.eligibility.review_expires_at, time.max, tzinfo=UTC)
    return (
        source.llm_processing == "remote_allowed"
        and source.eligibility.remote_llm == "allow"
        and source.rights is not None
        and expires_at > generated_at
    )


def _editorial_provider(editorial: EditorialConfig) -> StructuredProvider:
    """Build the provider the configuration names; never fall back to the other route."""

    if editorial.remote_processing:
        return OpenAIResponsesProvider(timeout=300)
    return OllamaStructuredProvider(host=editorial.ollama_host, timeout=300)


def _lead_passage(body: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", body.replace("\n", " "))
    lead = " ".join(sentence.strip() for sentence in sentences[:2] if sentence.strip())
    return " ".join(lead.split()[:100]) or body[:500]


def _story_signals(title: str, categories: tuple[str, ...]) -> tuple[str, ...]:
    signals = {f"category:{category.casefold()}" for category in categories if category.strip()}
    title_tokens = re.findall(r"[^\W_]+", title, flags=re.UNICODE)
    signals.update(
        f"entity:{token.casefold()}"
        for index, token in enumerate(title_tokens)
        if (index > 0 and len(token) >= 4 and token[:1].isupper()) or token.isdigit()
    )
    return tuple(sorted(signals))
