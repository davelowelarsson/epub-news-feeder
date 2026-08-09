from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from types import TracebackType
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


@dataclass(frozen=True, slots=True)
class ArticleObservation:
    article_id: str
    revision_hash: str
    eligible: bool
    materially_changed: bool


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


def _token_hashes(text: str) -> list[str]:
    return [_hash(f"epub-news-feeder:v1:{token}") for token in normalize_text(text).split()]


def _changed_words(previous: list[str], current: list[str]) -> int:
    changed = 0
    for tag, old_start, old_end, new_start, new_end in SequenceMatcher(
        None, previous, current, autojunk=False
    ).get_opcodes():
        if tag != "equal":
            changed += max(old_end - old_start, new_end - new_start)
    return changed


class StateStore:
    def __init__(self, path: Path, *, environment: str) -> None:
        self.path = path
        self.environment = environment
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> StateStore:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=1, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        self._connection = connection
        self._migrate()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

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
                failure_reason TEXT
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
    ) -> ArticleObservation:
        canonical = normalize_url(canonical_url)
        aliases = [f"url:{canonical}"]
        if guid:
            aliases.append(f"guid:{source_id}:{guid}")

        article_id = self._find_article(aliases)
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
                SET canonical_url = ?, title = ?, author = ?, last_seen_at = ?
                WHERE article_id = ?
                """,
                (canonical, title, author, observed_at.isoformat(), article_id),
            )
        for alias in aliases:
            self.connection.execute(
                "INSERT OR IGNORE INTO aliases(alias, article_id) VALUES (?, ?)",
                (alias, article_id),
            )

        normalized = normalize_text(normalized_body)
        revision_hash = _hash(normalized)
        hashes = _token_hashes(normalized)
        self.connection.execute(
            """
            INSERT OR IGNORE INTO revisions(
                article_id, revision_hash, observed_at, word_count, token_hashes
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (article_id, revision_hash, observed_at.isoformat(), len(hashes), json.dumps(hashes)),
        )

        eligible = True
        materially_changed = False
        if publication_id is not None:
            eligible, materially_changed = self._revision_eligibility(
                publication_id, article_id, revision_hash, hashes
            )
        return ArticleObservation(article_id, revision_hash, eligible, materially_changed)

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
            ORDER BY d.delivered_at DESC
            LIMIT 1
            """,
            (publication_id, article_id),
        ).fetchone()
        if delivered is None:
            return True, False
        previous_hashes = json.loads(str(delivered["token_hashes"]))
        if not isinstance(previous_hashes, list) or not all(
            isinstance(value, str) for value in previous_hashes
        ):
            raise RuntimeError("Invalid revision fingerprint")
        threshold = max(50, (int(delivered["word_count"]) * 15 + 99) // 100)
        materially_changed = _changed_words(previous_hashes, current_hashes) >= threshold
        return materially_changed, materially_changed

    def begin_run(
        self,
        run_id: str,
        publication_id: str,
        edition_id: str,
        started_at: datetime,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO runs(
                run_id, publication_id, edition_id, environment, started_at, status
            ) VALUES (?, ?, ?, ?, ?, 'started')
            """,
            (run_id, publication_id, edition_id, self.environment, started_at.isoformat()),
        )

    def reserve_articles(
        self,
        run_id: str,
        publication_id: str,
        observations: Iterable[ArticleObservation],
        expires_at: datetime,
    ) -> None:
        with self.connection:
            for observation in observations:
                self.connection.execute(
                    """
                    INSERT INTO reservations(
                        publication_id, article_id, revision_hash, run_id, expires_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        publication_id,
                        observation.article_id,
                        observation.revision_hash,
                        run_id,
                        expires_at.isoformat(),
                    ),
                )
            self.connection.execute(
                "UPDATE runs SET status = 'validated' WHERE run_id = ?", (run_id,)
            )

    def finalize_delivery(
        self,
        run_id: str,
        publication_id: str,
        delivered_at: datetime,
        delivery_digest: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO deliveries(
                    publication_id, article_id, revision_hash, run_id, delivered_at
                )
                SELECT publication_id, article_id, revision_hash, run_id, ?
                FROM reservations WHERE run_id = ? AND publication_id = ?
                """,
                (delivered_at.isoformat(), run_id, publication_id),
            )
            self.connection.execute("DELETE FROM reservations WHERE run_id = ?", (run_id,))
            self.connection.execute(
                "UPDATE runs SET status = 'delivered', delivery_digest = ? WHERE run_id = ?",
                (delivery_digest, run_id),
            )

    def abandon_run(self, run_id: str, *, reason: str) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM reservations WHERE run_id = ?", (run_id,))
            self.connection.execute(
                "UPDATE runs SET status = 'failed', failure_reason = ? WHERE run_id = ?",
                (reason, run_id),
            )

    def active_reservations(self, publication_id: str) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT article_id FROM reservations
            WHERE publication_id = ? ORDER BY article_id
            """,
            (publication_id,),
        ).fetchall()
        return [str(row["article_id"]) for row in rows]

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
