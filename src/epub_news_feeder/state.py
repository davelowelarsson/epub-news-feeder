from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import sqlite3
import unicodedata
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from types import TracebackType
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid"}

# How long a Near Miss stays recoverable. Two weeks comfortably covers the weekly's
# seven-day read window while keeping the table a window rather than an archive.
_NEAR_MISS_RETENTION = timedelta(days=14)


@dataclass(frozen=True, slots=True)
class ArticleObservation:
    article_id: str
    revision_hash: str
    eligible: bool
    materially_changed: bool
    correction_pending: bool = False


@dataclass(frozen=True, slots=True)
class CorrectionNotice:
    signal_id: str
    article_id: str
    revision_hash: str
    kind: str
    signaled_at: datetime
    title: str
    canonical_url: str
    publisher_id: str


@dataclass(frozen=True, slots=True)
class StoryCoverage:
    article_id: str
    title: str
    publisher_id: str
    canonical_url: str
    publisher_published_at: datetime
    delivered_at: datetime


@dataclass(frozen=True, slots=True)
class ClusterOverride:
    article_id: str
    cluster_id: str | None
    reason: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class PendingBrief:
    """A Brief carried by a validated-but-undelivered Run, staged for delivery recording.

    This is bookkeeping only: it lets a resumed Run recover which Briefs it selected without
    re-acquiring Sources. Suppression itself is decided solely by ``brief_deliveries``, written
    at finalization.
    """

    brief_id: str
    source_id: str
    published_at: datetime


@dataclass(frozen=True, slots=True)
class PendingDelivery:
    run_id: str
    publication_id: str
    edition_id: str
    delivery_target: str
    delivery_digest: str
    prepared_at: datetime
    briefs: tuple[PendingBrief, ...] = ()


def normalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        hostname = f"{hostname}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMETERS
        ]
    )
    return urlunsplit((scheme, hostname, path, query, ""))


def normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def brief_id(canonical_url: str) -> str:
    """A Brief's durable, body-free identity: the unkeyed hash of its normalized canonical URL.

    Mirrors ``article_id``'s derivation. Unkeyed deliberately — the fingerprint key exists for
    body token hashes, and a canonical URL is not publisher text, so keying it would tie Brief
    suppression to key survival for no privacy gain. Not the feed GUID either, so identity
    survives a Source changing its GUID scheme.
    """

    return _hash(normalize_url(canonical_url))[:24]


def _token_hashes(text: str, key: bytes) -> list[str]:
    return [
        hmac.new(
            key,
            f"epub-news-feeder:v2:{token}".encode(),
            hashlib.sha256,
        ).hexdigest()
        for token in normalize_text(text).split()
    ]


def _changed_words(previous: list[str], current: list[str]) -> int:
    changed = 0
    for tag, old_start, old_end, new_start, new_end in SequenceMatcher(
        None, previous, current, autojunk=False
    ).get_opcodes():
        if tag != "equal":
            changed += max(old_end - old_start, new_end - new_start)
    return changed


class StateStore:
    def __init__(
        self,
        path: Path,
        *,
        environment: str,
        near_duplicate_similarity: float = 0.97,
        near_duplicate_window: timedelta = timedelta(days=3),
        story_cluster_window: timedelta = timedelta(days=7),
        story_cluster_min_signals: int = 2,
    ) -> None:
        if not 0.0 <= near_duplicate_similarity <= 1.0:
            raise ValueError("near_duplicate_similarity must be between 0 and 1")
        if near_duplicate_window < timedelta(0):
            raise ValueError("near_duplicate_window must not be negative")
        if story_cluster_window < timedelta(0):
            raise ValueError("story_cluster_window must not be negative")
        if story_cluster_min_signals < 2:
            raise ValueError("story_cluster_min_signals must be at least 2")
        self.path = path
        self.environment = environment
        self.near_duplicate_similarity = near_duplicate_similarity
        self.near_duplicate_window = near_duplicate_window
        self.story_cluster_window = story_cluster_window
        self.story_cluster_min_signals = story_cluster_min_signals
        self._connection: sqlite3.Connection | None = None
        self._lock_fd: int | None = None
        self._fingerprint_key: bytes | None = None

    def __enter__(self) -> StateStore:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(lock_fd)
            raise RuntimeError(f"State Store is already open for writing: {self.path}") from error
        self._lock_fd = lock_fd
        try:
            self._fingerprint_key = self._load_or_create_fingerprint_key()
            database_fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            os.close(database_fd)
            os.chmod(self.path, 0o600)
            connection = sqlite3.connect(self.path, timeout=1, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            self._connection = connection
            self._migrate()
            return self
        except BaseException:
            self._release_writer_lock()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._secure_sidecar_permissions()
        self._fingerprint_key = None
        self._release_writer_lock()

    @property
    def fingerprint_key(self) -> bytes:
        if self._fingerprint_key is None:
            raise RuntimeError("StateStore is not open")
        return self._fingerprint_key

    def _load_or_create_fingerprint_key(self) -> bytes:
        key_path = self.path.with_suffix(f"{self.path.suffix}.key")
        try:
            key_fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                os.write(key_fd, os.urandom(32))
            finally:
                os.close(key_fd)
        os.chmod(key_path, 0o600)
        key = key_path.read_bytes()
        if len(key) != 32:
            raise RuntimeError(f"Invalid State Store fingerprint key: {key_path}")
        return key

    def _secure_sidecar_permissions(self) -> None:
        for path in (
            self.path,
            self.path.with_suffix(f"{self.path.suffix}.key"),
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
            self.path.with_suffix(f"{self.path.suffix}.lock"),
        ):
            if path.exists():
                path.chmod(0o600)

    def _release_writer_lock(self) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("StateStore is not open")
        return self._connection

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY
            );
            INSERT OR IGNORE INTO schema_migrations(version) VALUES (1);

            CREATE TABLE IF NOT EXISTS articles (
                article_id TEXT PRIMARY KEY,
                canonical_url TEXT NOT NULL UNIQUE,
                publisher_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                guid TEXT,
                title TEXT NOT NULL,
                author TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS aliases (
                alias TEXT PRIMARY KEY,
                article_id TEXT NOT NULL REFERENCES articles(article_id)
            );
            CREATE TABLE IF NOT EXISTS revisions (
                article_id TEXT NOT NULL REFERENCES articles(article_id),
                revision_hash TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                word_count INTEGER NOT NULL,
                token_hashes TEXT NOT NULL,
                fingerprint_version INTEGER NOT NULL DEFAULT 2,
                PRIMARY KEY(article_id, revision_hash)
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                publication_id TEXT NOT NULL,
                edition_id TEXT NOT NULL,
                environment TEXT NOT NULL,
                started_at TEXT NOT NULL,
                status TEXT NOT NULL,
                delivery_digest TEXT,
                failure_reason TEXT,
                article_count INTEGER NOT NULL DEFAULT 0,
                publisher_link_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS reservations (
                publication_id TEXT NOT NULL,
                article_id TEXT NOT NULL REFERENCES articles(article_id),
                revision_hash TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                expires_at TEXT NOT NULL,
                PRIMARY KEY(publication_id, article_id)
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                publication_id TEXT NOT NULL,
                article_id TEXT NOT NULL REFERENCES articles(article_id),
                revision_hash TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                delivered_at TEXT NOT NULL,
                PRIMARY KEY(publication_id, article_id, revision_hash)
            );
            CREATE TABLE IF NOT EXISTS source_health (
                source_id TEXT PRIMARY KEY,
                last_attempt TEXT NOT NULL,
                last_success TEXT,
                consecutive_failures INTEGER NOT NULL,
                response_classification TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discovery_provenance (
                provenance_id TEXT PRIMARY KEY,
                article_id TEXT NOT NULL REFERENCES articles(article_id),
                source_id TEXT NOT NULL,
                publisher_id TEXT NOT NULL,
                url TEXT NOT NULL,
                guid TEXT,
                discovered_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS correction_signals (
                publication_id TEXT NOT NULL,
                signal_id TEXT NOT NULL,
                article_id TEXT NOT NULL REFERENCES articles(article_id),
                revision_hash TEXT NOT NULL,
                kind TEXT NOT NULL,
                signaled_at TEXT NOT NULL,
                delivered_at TEXT,
                PRIMARY KEY(publication_id, signal_id)
            );
            CREATE TABLE IF NOT EXISTS story_clusters (
                cluster_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS story_articles (
                article_id TEXT PRIMARY KEY REFERENCES articles(article_id),
                observed_at TEXT NOT NULL,
                cluster_id TEXT REFERENCES story_clusters(cluster_id)
            );
            CREATE TABLE IF NOT EXISTS story_signals (
                article_id TEXT NOT NULL REFERENCES articles(article_id),
                signal TEXT NOT NULL,
                PRIMARY KEY(article_id, signal)
            );
            CREATE TABLE IF NOT EXISTS cluster_overrides (
                override_id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id TEXT NOT NULL REFERENCES articles(article_id),
                cluster_id TEXT REFERENCES story_clusters(cluster_id),
                reason TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cluster_coverage (
                publication_id TEXT NOT NULL,
                cluster_id TEXT NOT NULL REFERENCES story_clusters(cluster_id),
                article_id TEXT NOT NULL REFERENCES articles(article_id),
                title TEXT NOT NULL,
                publisher_id TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                publisher_published_at TEXT NOT NULL,
                delivered_at TEXT NOT NULL,
                PRIMARY KEY(publication_id, cluster_id, article_id)
            );
            CREATE TABLE IF NOT EXISTS pending_deliveries (
                run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                publication_id TEXT NOT NULL,
                edition_id TEXT NOT NULL,
                delivery_target TEXT NOT NULL,
                delivery_digest TEXT NOT NULL,
                prepared_at TEXT NOT NULL,
                UNIQUE(publication_id, edition_id, delivery_target)
            );
            CREATE TABLE IF NOT EXISTS brief_deliveries (
                publication_id TEXT NOT NULL,
                brief_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                published_at TEXT NOT NULL,
                delivered_at TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                PRIMARY KEY(publication_id, brief_id)
            );
            INSERT OR IGNORE INTO schema_migrations(version) VALUES (2);
            """
        )
        revision_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(revisions)").fetchall()
        }
        if "fingerprint_version" not in revision_columns:
            self.connection.execute(
                "ALTER TABLE revisions ADD COLUMN fingerprint_version INTEGER NOT NULL DEFAULT 1"
            )
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (3)")
        run_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        if "article_count" not in run_columns:
            self.connection.execute(
                "ALTER TABLE runs ADD COLUMN article_count INTEGER NOT NULL DEFAULT 0"
            )
        if "publisher_link_count" not in run_columns:
            self.connection.execute(
                "ALTER TABLE runs ADD COLUMN publisher_link_count INTEGER NOT NULL DEFAULT 0"
            )
        self.connection.execute(
            """
            UPDATE runs
            SET article_count = (
                SELECT COUNT(DISTINCT deliveries.article_id)
                FROM deliveries WHERE deliveries.run_id = runs.run_id
            )
            WHERE status = 'delivered' AND article_count = 0 AND publisher_link_count = 0
            """
        )
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (4)")
        pending_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(pending_deliveries)").fetchall()
        }
        if "briefs" not in pending_columns:
            self.connection.execute(
                "ALTER TABLE pending_deliveries ADD COLUMN briefs TEXT NOT NULL DEFAULT '[]'"
            )
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (5)")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS near_misses (
                publication_id TEXT NOT NULL,
                article_id TEXT NOT NULL REFERENCES articles(article_id),
                source_id TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY(publication_id, article_id)
            );
            INSERT OR IGNORE INTO schema_migrations(version) VALUES (6);
            """
        )

    def observe_article(
        self,
        *,
        source_id: str,
        publisher_id: str,
        canonical_url: str,
        guid: str | None,
        title: str,
        author: str | None,
        normalized_body: str,
        observed_at: datetime,
        publication_id: str | None = None,
        run_id: str | None = None,
        correction_signal_id: str | None = None,
        correction_kind: str | None = None,
    ) -> ArticleObservation:
        if (correction_signal_id is None) != (correction_kind is None):
            raise ValueError("correction_signal_id and correction_kind must be provided together")
        if correction_signal_id is not None and publication_id is None:
            raise ValueError("correction signals require publication_id")
        canonical = normalize_url(canonical_url)
        aliases = [f"url:{canonical}"]
        if guid:
            aliases.append(f"guid:{source_id}:{guid}")

        normalized = normalize_text(normalized_body)
        revision_hash = _hash(normalized)
        hashes = _token_hashes(normalized, self.fingerprint_key)
        article_id = self._find_article(aliases)
        if article_id is None and hashes:
            article_id = self._find_exact_body(revision_hash)
        if article_id is None and len(hashes) >= 100:
            article_id = self._find_near_duplicate(publisher_id, hashes, observed_at)
        if article_id is None:
            article_id = _hash(canonical)[:24]
            self.connection.execute(
                """
                INSERT INTO articles(
                    article_id, canonical_url, publisher_id, source_id, guid,
                    title, author, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    article_id,
                    canonical,
                    publisher_id,
                    source_id,
                    guid,
                    title,
                    author,
                    observed_at.isoformat(),
                    observed_at.isoformat(),
                ),
            )
        else:
            self.connection.execute(
                """
                UPDATE articles
                SET title = ?, author = ?, last_seen_at = MAX(last_seen_at, ?)
                WHERE article_id = ?
                """,
                (title, author, observed_at.isoformat(), article_id),
            )
        for alias in aliases:
            self.connection.execute(
                "INSERT OR IGNORE INTO aliases(alias, article_id) VALUES (?, ?)",
                (alias, article_id),
            )
        provenance_id = _hash(
            "\x1f".join((article_id, source_id, publisher_id, canonical, guid or ""))
        )
        self.connection.execute(
            """
            INSERT OR IGNORE INTO discovery_provenance(
                provenance_id, article_id, source_id, publisher_id, url, guid, discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provenance_id,
                article_id,
                source_id,
                publisher_id,
                canonical,
                guid,
                observed_at.isoformat(),
            ),
        )

        self.connection.execute(
            """
            INSERT INTO revisions(
                article_id, revision_hash, observed_at, word_count, token_hashes,
                fingerprint_version
            ) VALUES (?, ?, ?, ?, ?, 2)
            ON CONFLICT(article_id, revision_hash) DO UPDATE SET
                word_count = excluded.word_count,
                token_hashes = excluded.token_hashes,
                fingerprint_version = excluded.fingerprint_version
            """,
            (article_id, revision_hash, observed_at.isoformat(), len(hashes), json.dumps(hashes)),
        )

        if correction_signal_id is not None and correction_kind is not None:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO correction_signals(
                    publication_id, signal_id, article_id, revision_hash, kind, signaled_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    publication_id,
                    correction_signal_id,
                    article_id,
                    revision_hash,
                    correction_kind,
                    observed_at.isoformat(),
                ),
            )

        eligible = True
        materially_changed = False
        correction_pending = False
        if publication_id is not None:
            self.cleanup_expired_reservations(observed_at)
            reservation_run_id = self._active_reservation_run(publication_id, article_id)
            reserved = reservation_run_id is not None and reservation_run_id != run_id
            eligible, materially_changed = self._revision_eligibility(
                publication_id, article_id, revision_hash, hashes
            )
            if reserved:
                eligible = False
                materially_changed = False
            correction_pending = self._has_pending_correction(
                publication_id, article_id, revision_hash
            )
            if (
                correction_pending
                and not self._was_revision_delivered(publication_id, article_id, revision_hash)
                and not reserved
            ):
                eligible = True
        return ArticleObservation(
            article_id, revision_hash, eligible, materially_changed, correction_pending
        )

    def _find_exact_body(self, revision_hash: str) -> str | None:
        row = self.connection.execute(
            """
            SELECT a.article_id
            FROM revisions r
            JOIN articles a ON a.article_id = r.article_id
            WHERE r.revision_hash = ?
            ORDER BY a.first_seen_at, a.article_id
            LIMIT 1
            """,
            (revision_hash,),
        ).fetchone()
        return None if row is None else str(row["article_id"])

    def _find_near_duplicate(
        self, publisher_id: str, current_hashes: list[str], observed_at: datetime
    ) -> str | None:
        window_start = observed_at - self.near_duplicate_window
        rows = self.connection.execute(
            """
            SELECT DISTINCT a.article_id, r.token_hashes, a.first_seen_at, r.observed_at,
                            r.revision_hash
            FROM articles a
            JOIN revisions r ON r.article_id = a.article_id
            JOIN discovery_provenance p ON p.article_id = a.article_id
            WHERE p.publisher_id = ?
              AND a.last_seen_at BETWEEN ? AND ?
            ORDER BY a.first_seen_at, a.article_id, r.observed_at, r.revision_hash
            """,
            (publisher_id, window_start.isoformat(), observed_at.isoformat()),
        ).fetchall()
        for row in rows:
            previous_hashes = self._load_token_hashes(str(row["token_hashes"]))
            if len(previous_hashes) < 100:
                continue
            similarity = SequenceMatcher(
                None, previous_hashes, current_hashes, autojunk=False
            ).ratio()
            if similarity >= self.near_duplicate_similarity:
                return str(row["article_id"])
        return None

    def discovery_provenance(self, article_id: str) -> list[dict[str, str | None]]:
        rows = self.connection.execute(
            """
            SELECT source_id, publisher_id, url, guid, discovered_at
            FROM discovery_provenance
            WHERE article_id = ?
            ORDER BY discovered_at, provenance_id
            """,
            (article_id,),
        ).fetchall()
        return [
            {
                "source_id": str(row["source_id"]),
                "publisher_id": str(row["publisher_id"]),
                "url": str(row["url"]),
                "guid": None if row["guid"] is None else str(row["guid"]),
                "discovered_at": str(row["discovered_at"]),
            }
            for row in rows
        ]

    @staticmethod
    def _load_token_hashes(serialized: str) -> list[str]:
        hashes = json.loads(serialized)
        if not isinstance(hashes, list) or not all(isinstance(value, str) for value in hashes):
            raise RuntimeError("Invalid revision fingerprint")
        return hashes

    def _find_article(self, aliases: Iterable[str]) -> str | None:
        for alias in aliases:
            row = self.connection.execute(
                "SELECT article_id FROM aliases WHERE alias = ?", (alias,)
            ).fetchone()
            if row is not None:
                return str(row["article_id"])
        return None

    def _revision_eligibility(
        self,
        publication_id: str,
        article_id: str,
        revision_hash: str,
        current_hashes: list[str],
    ) -> tuple[bool, bool]:
        delivered_exact = self.connection.execute(
            """
            SELECT 1 FROM deliveries
            WHERE publication_id = ? AND article_id = ? AND revision_hash = ?
            """,
            (publication_id, article_id, revision_hash),
        ).fetchone()
        if delivered_exact is not None:
            return False, False

        delivered = self.connection.execute(
            """
            SELECT r.token_hashes, r.word_count
            FROM deliveries d
            JOIN revisions r
              ON r.article_id = d.article_id AND r.revision_hash = d.revision_hash
            WHERE d.publication_id = ? AND d.article_id = ?
            ORDER BY d.delivered_at DESC, d.run_id DESC, d.revision_hash DESC
            LIMIT 1
            """,
            (publication_id, article_id),
        ).fetchone()
        if delivered is None:
            return True, False
        previous_hashes = self._load_token_hashes(str(delivered["token_hashes"]))
        # A body that lost half its words since delivery is an extraction artifact — a
        # paywall teaser or a chrome-only scrape — not publisher news. Re-delivering it
        # would replace a full article the reader already has with a stub labelled
        # "updated", so a shrunken observation is never material however large its diff.
        delivered_word_count = int(delivered["word_count"])
        if len(current_hashes) * 2 < delivered_word_count:
            return False, False
        threshold = max(50, (delivered_word_count * 15 + 99) // 100)
        materially_changed = _changed_words(previous_hashes, current_hashes) >= threshold
        return materially_changed, materially_changed

    def _was_revision_delivered(
        self, publication_id: str, article_id: str, revision_hash: str
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM deliveries
            WHERE publication_id = ? AND article_id = ? AND revision_hash = ?
            """,
            (publication_id, article_id, revision_hash),
        ).fetchone()
        return row is not None

    def _has_pending_correction(
        self, publication_id: str, article_id: str, revision_hash: str
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM correction_signals
            WHERE publication_id = ? AND article_id = ? AND revision_hash = ?
              AND delivered_at IS NULL
            """,
            (publication_id, article_id, revision_hash),
        ).fetchone()
        return row is not None

    def pending_corrections(self, publication_id: str) -> list[CorrectionNotice]:
        rows = self.connection.execute(
            """
            SELECT c.signal_id, c.article_id, c.revision_hash, c.kind, c.signaled_at,
                   a.title, a.canonical_url, a.publisher_id
            FROM correction_signals c
            JOIN articles a ON a.article_id = c.article_id
            WHERE publication_id = ? AND delivered_at IS NULL
            ORDER BY signaled_at, signal_id
            """,
            (publication_id,),
        ).fetchall()
        return [
            CorrectionNotice(
                signal_id=str(row["signal_id"]),
                article_id=str(row["article_id"]),
                revision_hash=str(row["revision_hash"]),
                kind=str(row["kind"]),
                signaled_at=datetime.fromisoformat(str(row["signaled_at"])),
                title=str(row["title"]),
                canonical_url=str(row["canonical_url"]),
                publisher_id=str(row["publisher_id"]),
            )
            for row in rows
        ]

    def acknowledge_corrections(
        self,
        publication_id: str,
        signal_ids: Iterable[str],
        *,
        delivered_at: datetime,
    ) -> None:
        with self.connection:
            for signal_id in signal_ids:
                self.connection.execute(
                    """
                    UPDATE correction_signals SET delivered_at = ?
                    WHERE publication_id = ? AND signal_id = ? AND delivered_at IS NULL
                    """,
                    (delivered_at.isoformat(), publication_id, signal_id),
                )

    @staticmethod
    def deterministic_cluster_id(first_article_id: str, second_article_id: str) -> str:
        pair = "\x1f".join(sorted((first_article_id, second_article_id)))
        return _hash(f"story-cluster:v1:{pair}")[:24]

    def match_story_cluster(
        self,
        article_id: str,
        *,
        signals: Iterable[str],
        observed_at: datetime,
    ) -> str | None:
        normalized_signals = sorted(
            {normalize_text(signal).casefold() for signal in signals if normalize_text(signal)}
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO story_articles(article_id, observed_at, cluster_id)
                VALUES (?, ?, NULL)
                ON CONFLICT(article_id) DO UPDATE SET observed_at = excluded.observed_at
                """,
                (article_id, observed_at.isoformat()),
            )
            self.connection.execute("DELETE FROM story_signals WHERE article_id = ?", (article_id,))
            self.connection.executemany(
                "INSERT INTO story_signals(article_id, signal) VALUES (?, ?)",
                [(article_id, signal) for signal in normalized_signals],
            )

            override = self.connection.execute(
                """
                SELECT cluster_id FROM cluster_overrides
                WHERE article_id = ?
                ORDER BY recorded_at DESC, override_id DESC LIMIT 1
                """,
                (article_id,),
            ).fetchone()
            if override is not None:
                forced_cluster_id = (
                    None if override["cluster_id"] is None else str(override["cluster_id"])
                )
                self.connection.execute(
                    "UPDATE story_articles SET cluster_id = ? WHERE article_id = ?",
                    (forced_cluster_id, article_id),
                )
                return forced_cluster_id

            existing = self.story_cluster(article_id)
            if existing is not None:
                return existing
            if len(normalized_signals) < self.story_cluster_min_signals:
                return None

            rows = self.connection.execute(
                """
                SELECT article_id, observed_at, cluster_id
                FROM story_articles
                WHERE article_id != ? AND observed_at BETWEEN ? AND ?
                ORDER BY observed_at, article_id
                """,
                (
                    article_id,
                    (observed_at - self.story_cluster_window).isoformat(),
                    observed_at.isoformat(),
                ),
            ).fetchall()
            current = set(normalized_signals)
            matches: list[tuple[int, str, str, str | None]] = []
            for row in rows:
                candidate_id = str(row["article_id"])
                candidate_rows = self.connection.execute(
                    "SELECT signal FROM story_signals WHERE article_id = ?",
                    (candidate_id,),
                ).fetchall()
                shared = len(current & {str(item["signal"]) for item in candidate_rows})
                if shared >= self.story_cluster_min_signals:
                    matches.append(
                        (
                            -shared,
                            str(row["observed_at"]),
                            candidate_id,
                            None if row["cluster_id"] is None else str(row["cluster_id"]),
                        )
                    )
            if not matches:
                return None

            _, _, candidate_id, candidate_cluster_id = min(matches)
            cluster_id = candidate_cluster_id or self.deterministic_cluster_id(
                candidate_id, article_id
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO story_clusters(cluster_id, created_at) VALUES (?, ?)",
                (cluster_id, observed_at.isoformat()),
            )
            self.connection.execute(
                "UPDATE story_articles SET cluster_id = ? WHERE article_id IN (?, ?)",
                (cluster_id, candidate_id, article_id),
            )
            return cluster_id

    def story_cluster(self, article_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT cluster_id FROM story_articles WHERE article_id = ?", (article_id,)
        ).fetchone()
        if row is None or row["cluster_id"] is None:
            return None
        return str(row["cluster_id"])

    def story_cluster_members(self, cluster_id: str) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT article_id FROM story_articles
            WHERE cluster_id = ? ORDER BY observed_at, article_id
            """,
            (cluster_id,),
        ).fetchall()
        return [str(row["article_id"]) for row in rows]

    def set_cluster_override(
        self,
        article_id: str,
        *,
        cluster_id: str | None,
        reason: str,
        recorded_at: datetime,
    ) -> None:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE story_articles SET cluster_id = ? WHERE article_id = ?",
                (cluster_id, article_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Article has no story metadata: {article_id}")
            self.connection.execute(
                """
                INSERT INTO cluster_overrides(article_id, cluster_id, reason, recorded_at)
                VALUES (?, ?, ?, ?)
                """,
                (article_id, cluster_id, reason, recorded_at.isoformat()),
            )

    def cluster_override_history(self, article_id: str) -> list[ClusterOverride]:
        rows = self.connection.execute(
            """
            SELECT article_id, cluster_id, reason, recorded_at
            FROM cluster_overrides
            WHERE article_id = ? ORDER BY recorded_at, override_id
            """,
            (article_id,),
        ).fetchall()
        return [
            ClusterOverride(
                article_id=str(row["article_id"]),
                cluster_id=(None if row["cluster_id"] is None else str(row["cluster_id"])),
                reason=str(row["reason"]),
                recorded_at=datetime.fromisoformat(str(row["recorded_at"])),
            )
            for row in rows
        ]

    def record_cluster_delivery(
        self,
        *,
        publication_id: str,
        cluster_id: str,
        article_id: str,
        title: str,
        publisher_id: str,
        canonical_url: str,
        publisher_published_at: datetime,
        delivered_at: datetime,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO cluster_coverage(
                publication_id, cluster_id, article_id, title, publisher_id,
                canonical_url, publisher_published_at, delivered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                publication_id,
                cluster_id,
                article_id,
                title,
                publisher_id,
                normalize_url(canonical_url),
                publisher_published_at.isoformat(),
                delivered_at.isoformat(),
            ),
        )

    def prior_cluster_coverage(
        self, publication_id: str, cluster_id: str, *, limit: int = 10
    ) -> list[StoryCoverage]:
        if limit < 0:
            raise ValueError("limit must not be negative")
        rows = self.connection.execute(
            """
            SELECT article_id, title, publisher_id, canonical_url,
                   publisher_published_at, delivered_at
            FROM cluster_coverage
            WHERE publication_id = ? AND cluster_id = ?
            ORDER BY delivered_at DESC, article_id
            LIMIT ?
            """,
            (publication_id, cluster_id, limit),
        ).fetchall()
        return [
            StoryCoverage(
                article_id=str(row["article_id"]),
                title=str(row["title"]),
                publisher_id=str(row["publisher_id"]),
                canonical_url=str(row["canonical_url"]),
                publisher_published_at=datetime.fromisoformat(str(row["publisher_published_at"])),
                delivered_at=datetime.fromisoformat(str(row["delivered_at"])),
            )
            for row in rows
        ]

    def delivered_article_ids(self, publication_ids: Iterable[str]) -> frozenset[str]:
        """Every Article any of *publication_ids* has delivered, at any revision.

        Deliberately coarser than ``_revision_eligibility``, which lets a materially revised
        Article come back to the same Publication. Across Publications the question is not
        whether the text moved but whether the reader already read the story, and a Saturday
        Edition that reprints Wednesday's news with three sentences changed has failed at
        being a Saturday Edition.
        """

        identifiers = tuple(dict.fromkeys(publication_ids))
        if not identifiers:
            return frozenset()
        placeholders = ",".join("?" * len(identifiers))
        rows = self.connection.execute(
            f"SELECT DISTINCT article_id FROM deliveries WHERE publication_id IN ({placeholders})",
            identifiers,
        ).fetchall()
        return frozenset(str(row["article_id"]) for row in rows)

    def recent_deliveries_by_title(
        self, publication_ids: Iterable[str], *, since: datetime
    ) -> dict[tuple[str, str], set[str]]:
        """Delivered Article identities since *since*, keyed by publisher and normalized title.

        Exists for republished-title suppression: a publisher re-issuing a delivered story at
        a new URL creates a new identity, so identity-based history suppression cannot see it.
        Titles come from ``articles``, which tracks the latest observed title — good enough,
        because a republication carries the headline it wants recognized by.
        """

        identifiers = tuple(dict.fromkeys(publication_ids))
        if not identifiers:
            return {}
        placeholders = ",".join("?" * len(identifiers))
        rows = self.connection.execute(
            f"""
            SELECT DISTINCT a.article_id, a.publisher_id, a.title
            FROM deliveries d
            JOIN articles a ON a.article_id = d.article_id
            WHERE d.publication_id IN ({placeholders}) AND d.delivered_at >= ?
            """,
            (*identifiers, since.isoformat()),
        ).fetchall()
        delivered: dict[tuple[str, str], set[str]] = {}
        for row in rows:
            story = (str(row["publisher_id"]), normalize_text(str(row["title"])).casefold())
            delivered.setdefault(story, set()).add(str(row["article_id"]))
        return delivered

    def record_near_misses(
        self,
        publication_id: str,
        article_ids_with_sources: Iterable[tuple[str, str]],
        recorded_at: datetime,
    ) -> None:
        """Record this Run's Near Misses and prune every record past its recovery window.

        A Near Miss is a body-free pointer — an Article identity and the Source that carried
        it; the canonical URL already lives in ``articles`` and is joined on read. Re-recording
        moves ``recorded_at`` forward, because a Run that considered the Article again renewed
        its claim to recovery. Pruning rides along on every recording, opportunistically, so
        the table stays a two-week recovery window and never becomes an archive: a Near Miss
        nobody recovered inside the window was not worth a Saturday slot either.
        """

        with self._transaction():
            for article_id, source_id in article_ids_with_sources:
                self.connection.execute(
                    """
                    INSERT INTO near_misses(publication_id, article_id, source_id, recorded_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(publication_id, article_id) DO UPDATE SET
                        source_id = excluded.source_id,
                        recorded_at = excluded.recorded_at
                    """,
                    (publication_id, article_id, source_id, recorded_at.isoformat()),
                )
            self.connection.execute(
                "DELETE FROM near_misses WHERE recorded_at < ?",
                ((recorded_at - _NEAR_MISS_RETENTION).isoformat(),),
            )

    def near_misses(
        self, publication_ids: Iterable[str], *, since: datetime
    ) -> list[tuple[str, str, str]]:
        """Near Misses of *publication_ids* since *since*: (article_id, source_id, url) rows.

        Distinct by Article — several referenced Publications missing the same story is one
        recovery, not several fetches — keeping the most recently recorded row's Source.
        Newest recorded first, then Article identity, so a bounded recovery budget spends
        itself on the fresh end of the window deterministically.
        """

        identifiers = tuple(dict.fromkeys(publication_ids))
        if not identifiers:
            return []
        placeholders = ",".join("?" * len(identifiers))
        rows = self.connection.execute(
            f"""
            SELECT n.article_id, n.source_id, a.canonical_url,
                   MAX(n.recorded_at) AS recorded_at
            FROM near_misses n
            JOIN articles a ON a.article_id = n.article_id
            WHERE n.publication_id IN ({placeholders}) AND n.recorded_at >= ?
            GROUP BY n.article_id
            ORDER BY recorded_at DESC, n.article_id
            """,
            (*identifiers, since.isoformat()),
        ).fetchall()
        return [
            (str(row["article_id"]), str(row["source_id"]), str(row["canonical_url"]))
            for row in rows
        ]

    def cluster_recurrence(
        self, publication_ids: Iterable[str], *, since: datetime | None = None
    ) -> dict[str, int]:
        """Per Story Cluster, on how many distinct days those Publications delivered into it.

        Distinct days rather than Article count, because recurrence is the signal being
        measured: a story four publishers covered on one morning recurred once, while a story
        one publisher returned to on four mornings is the week's continuing thread.

        Read from ``deliveries`` joined to the global ``story_articles``, deliberately, and not
        from ``cluster_coverage``. Coverage exists to render a Story Hub and is incomplete in
        three ways that do not matter for display and matter entirely for ordering: its
        ``INSERT OR IGNORE`` pins ``delivered_at`` to an Article's first delivery, so a story
        the Publication returned to contributes one dated row rather than several; the
        spool-resume path finalizes a Delivery without writing coverage at all; and Articles
        from Sources whose feeds carry no publication date are skipped outright.

        ``deliveries`` has none of those properties. Every finalized Delivery writes to it on
        every path, its key includes the revision so a re-delivered Article contributes its own
        row and its own date, and it is indifferent to whether a publisher dated anything.
        """

        identifiers = tuple(dict.fromkeys(publication_ids))
        if not identifiers:
            return {}
        placeholders = ",".join("?" * len(identifiers))
        parameters: list[str] = list(identifiers)
        window = ""
        if since is not None:
            window = " AND deliveries.delivered_at >= ?"
            parameters.append(since.isoformat())
        rows = self.connection.execute(
            f"""
            SELECT story_articles.cluster_id AS cluster_id,
                   COUNT(DISTINCT substr(deliveries.delivered_at, 1, 10)) AS days
            FROM deliveries
            JOIN story_articles ON story_articles.article_id = deliveries.article_id
            WHERE deliveries.publication_id IN ({placeholders})
              AND story_articles.cluster_id IS NOT NULL{window}
            GROUP BY story_articles.cluster_id
            """,
            parameters,
        ).fetchall()
        return {str(row["cluster_id"]): int(row["days"]) for row in rows}

    def begin_run(
        self,
        run_id: str,
        publication_id: str,
        edition_id: str,
        started_at: datetime,
    ) -> None:
        values = (
            publication_id,
            edition_id,
            self.environment,
            started_at.isoformat(),
        )
        existing = self.connection.execute(
            """
            SELECT publication_id, edition_id, environment, started_at
            FROM runs WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if existing is not None:
            actual = tuple(
                str(existing[column])
                for column in ("publication_id", "edition_id", "environment", "started_at")
            )
            if actual != values:
                raise RuntimeError(f"Run ID already has different immutable metadata: {run_id}")
            return
        self.connection.execute(
            """
            INSERT INTO runs(
                run_id, publication_id, edition_id, environment, started_at, status
            ) VALUES (?, ?, ?, ?, ?, 'started')
            """,
            (run_id, *values),
        )

    def reserve_articles(
        self,
        run_id: str,
        publication_id: str,
        observations: Iterable[ArticleObservation],
        expires_at: datetime,
        *,
        article_count: int | None = None,
        publisher_link_count: int = 0,
    ) -> None:
        run = self.connection.execute(
            """
            SELECT publication_id, status, article_count, publisher_link_count
            FROM runs WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if run is None or str(run["publication_id"]) != publication_id:
            raise RuntimeError(f"Unknown Run for Publication: {run_id}")
        if str(run["status"]) not in {"started", "validated"}:
            raise RuntimeError(f"Run cannot reserve Articles in status {run['status']}")
        reserved_observations = tuple(observations)
        if article_count is None:
            article_count = len(reserved_observations)
        if article_count < 0 or publisher_link_count < 0:
            raise ValueError("Run item counts must not be negative")
        if str(run["status"]) == "validated" and (
            int(run["article_count"]),
            int(run["publisher_link_count"]),
        ) != (article_count, publisher_link_count):
            raise RuntimeError("Validated Run item counts are immutable")
        with self._transaction():
            for observation in reserved_observations:
                existing = self.connection.execute(
                    """
                    SELECT revision_hash, run_id, expires_at FROM reservations
                    WHERE publication_id = ? AND article_id = ?
                    """,
                    (publication_id, observation.article_id),
                ).fetchone()
                reservation = (
                    observation.revision_hash,
                    run_id,
                    expires_at.isoformat(),
                )
                if existing is not None:
                    actual = (
                        str(existing["revision_hash"]),
                        str(existing["run_id"]),
                        str(existing["expires_at"]),
                    )
                    if actual != reservation:
                        raise RuntimeError(
                            f"Article already reserved by another immutable Run: "
                            f"{observation.article_id}"
                        )
                    continue
                self.connection.execute(
                    """
                    INSERT INTO reservations(
                        publication_id, article_id, revision_hash, run_id, expires_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        publication_id,
                        observation.article_id,
                        *reservation,
                    ),
                )
            self.connection.execute(
                """
                UPDATE runs
                SET status = 'validated', article_count = ?, publisher_link_count = ?
                WHERE run_id = ?
                """,
                (article_count, publisher_link_count, run_id),
            )

    def finalize_delivery(
        self,
        run_id: str,
        publication_id: str,
        delivered_at: datetime,
        delivery_digest: str,
    ) -> None:
        run = self.connection.execute(
            "SELECT publication_id, status, delivery_digest FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run is None or str(run["publication_id"]) != publication_id:
            raise RuntimeError(f"Unknown Run for Publication: {run_id}")
        if str(run["status"]) == "delivered":
            if str(run["delivery_digest"]) != delivery_digest:
                raise RuntimeError("Delivered Edition digest is immutable")
            return
        if str(run["status"]) != "validated":
            raise RuntimeError(f"Run cannot finalize delivery in status {run['status']}")
        pending = self.connection.execute(
            "SELECT delivery_digest FROM pending_deliveries WHERE run_id = ?", (run_id,)
        ).fetchone()
        if pending is not None and str(pending["delivery_digest"]) != delivery_digest:
            raise RuntimeError("Pending Edition digest is immutable")
        with self._transaction():
            self.connection.execute(
                """
                INSERT OR IGNORE INTO deliveries(
                    publication_id, article_id, revision_hash, run_id, delivered_at
                )
                SELECT publication_id, article_id, revision_hash, run_id, ?
                FROM reservations WHERE run_id = ? AND publication_id = ?
                """,
                (delivered_at.isoformat(), run_id, publication_id),
            )
            self.connection.execute("DELETE FROM reservations WHERE run_id = ?", (run_id,))
            self.connection.execute("DELETE FROM pending_deliveries WHERE run_id = ?", (run_id,))
            self.connection.execute(
                "UPDATE runs SET status = 'delivered', delivery_digest = ? WHERE run_id = ?",
                (delivery_digest, run_id),
            )

    def abandon_run(self, run_id: str, *, reason: str) -> None:
        run = self.connection.execute(
            "SELECT status FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is not None and str(run["status"]) == "delivered":
            return
        with self._transaction():
            self.connection.execute("DELETE FROM reservations WHERE run_id = ?", (run_id,))
            self.connection.execute("DELETE FROM pending_deliveries WHERE run_id = ?", (run_id,))
            self.connection.execute(
                "UPDATE runs SET status = 'failed', failure_reason = ? WHERE run_id = ?",
                (reason, run_id),
            )

    def cleanup_expired_reservations(self, as_of: datetime) -> int:
        cursor = self.connection.execute(
            "DELETE FROM reservations WHERE expires_at <= ?", (as_of.isoformat(),)
        )
        return cursor.rowcount

    def _has_active_reservation(self, publication_id: str, article_id: str) -> bool:
        return self._active_reservation_run(publication_id, article_id) is not None

    def _active_reservation_run(self, publication_id: str, article_id: str) -> str | None:
        row = self.connection.execute(
            """
            SELECT run_id FROM reservations WHERE publication_id = ? AND article_id = ?
            """,
            (publication_id, article_id),
        ).fetchone()
        return None if row is None else str(row["run_id"])

    def active_reservations(
        self, publication_id: str, *, as_of: datetime | None = None
    ) -> list[str]:
        if as_of is not None:
            self.cleanup_expired_reservations(as_of)
        rows = self.connection.execute(
            """
            SELECT article_id FROM reservations
            WHERE publication_id = ? ORDER BY article_id
            """,
            (publication_id,),
        ).fetchall()
        return [str(row["article_id"]) for row in rows]

    def delivered_article_count(self, run_id: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(DISTINCT article_id) AS total FROM deliveries WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return 0 if row is None else int(row["total"])

    def run_item_counts(self, run_id: str) -> tuple[int, int]:
        row = self.connection.execute(
            "SELECT article_count, publisher_link_count FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Unknown Run: {run_id}")
        return int(row["article_count"]), int(row["publisher_link_count"])

    def run_delivery_status(self, run_id: str) -> tuple[str, str | None]:
        row = self.connection.execute(
            "SELECT status, delivery_digest FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Unknown Run: {run_id}")
        digest = row["delivery_digest"]
        return str(row["status"]), None if digest is None else str(digest)

    def prepare_delivery(
        self,
        *,
        run_id: str,
        publication_id: str,
        delivery_target: str,
        delivery_digest: str,
        prepared_at: datetime,
        briefs: Iterable[PendingBrief] = (),
    ) -> PendingDelivery:
        run = self.connection.execute(
            "SELECT publication_id, edition_id, status FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None or str(run["publication_id"]) != publication_id:
            raise RuntimeError(f"Unknown Run for Publication: {run_id}")
        if str(run["status"]) != "validated":
            raise RuntimeError(f"Run cannot prepare delivery in status {run['status']}")
        delivery = PendingDelivery(
            run_id=run_id,
            publication_id=publication_id,
            edition_id=str(run["edition_id"]),
            delivery_target=delivery_target,
            delivery_digest=delivery_digest,
            prepared_at=prepared_at,
            briefs=tuple(briefs),
        )
        existing = self.connection.execute(
            """
            SELECT run_id, publication_id, edition_id, delivery_target,
                   delivery_digest, prepared_at, briefs
            FROM pending_deliveries WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if existing is not None:
            persisted = self._pending_delivery_from_row(existing)
            if persisted != delivery:
                raise RuntimeError("Pending Edition metadata is immutable")
            return persisted
        self.connection.execute(
            """
            INSERT INTO pending_deliveries(
                run_id, publication_id, edition_id, delivery_target,
                delivery_digest, prepared_at, briefs
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delivery.run_id,
                delivery.publication_id,
                delivery.edition_id,
                delivery.delivery_target,
                delivery.delivery_digest,
                delivery.prepared_at.isoformat(),
                json.dumps(
                    [
                        {
                            "brief_id": brief.brief_id,
                            "source_id": brief.source_id,
                            "published_at": brief.published_at.isoformat(),
                        }
                        for brief in delivery.briefs
                    ]
                ),
            ),
        )
        return delivery

    def pending_deliveries(self, publication_id: str) -> list[PendingDelivery]:
        rows = self.connection.execute(
            """
            SELECT run_id, publication_id, edition_id, delivery_target,
                   delivery_digest, prepared_at, briefs
            FROM pending_deliveries
            WHERE publication_id = ? ORDER BY prepared_at, run_id
            """,
            (publication_id,),
        ).fetchall()
        return [self._pending_delivery_from_row(row) for row in rows]

    @staticmethod
    def _pending_delivery_from_row(row: sqlite3.Row) -> PendingDelivery:
        return PendingDelivery(
            run_id=str(row["run_id"]),
            publication_id=str(row["publication_id"]),
            edition_id=str(row["edition_id"]),
            delivery_target=str(row["delivery_target"]),
            delivery_digest=str(row["delivery_digest"]),
            prepared_at=datetime.fromisoformat(str(row["prepared_at"])),
            briefs=tuple(
                PendingBrief(
                    brief_id=str(item["brief_id"]),
                    source_id=str(item["source_id"]),
                    published_at=datetime.fromisoformat(str(item["published_at"])),
                )
                for item in json.loads(str(row["briefs"]))
            ),
        )

    def delivered_brief_ids(self, publication_id: str) -> frozenset[str]:
        rows = self.connection.execute(
            "SELECT brief_id FROM brief_deliveries WHERE publication_id = ?",
            (publication_id,),
        ).fetchall()
        return frozenset(str(row["brief_id"]) for row in rows)

    def record_brief_delivery(
        self,
        *,
        publication_id: str,
        brief_id: str,
        source_id: str,
        published_at: datetime,
        delivered_at: datetime,
        run_id: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO brief_deliveries(
                publication_id, brief_id, source_id, published_at, delivered_at, run_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                publication_id,
                brief_id,
                source_id,
                published_at.isoformat(),
                delivered_at.isoformat(),
                run_id,
            ),
        )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def record_source_health(
        self,
        source_id: str,
        *,
        attempted_at: datetime,
        succeeded: bool,
        classification: str,
    ) -> None:
        row = self.connection.execute(
            "SELECT consecutive_failures, last_success FROM source_health WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        failures = 0 if succeeded else (int(row["consecutive_failures"]) + 1 if row else 1)
        last_success = (
            attempted_at.isoformat() if succeeded else (row["last_success"] if row else None)
        )
        self.connection.execute(
            """
            INSERT INTO source_health(
                source_id, last_attempt, last_success, consecutive_failures,
                response_classification
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                last_attempt = excluded.last_attempt,
                last_success = excluded.last_success,
                consecutive_failures = excluded.consecutive_failures,
                response_classification = excluded.response_classification
            """,
            (source_id, attempted_at.isoformat(), last_success, failures, classification),
        )
