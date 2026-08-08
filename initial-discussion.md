Below follows some initial dialog around this project

---

# Product Requirements Document (PRD): Family Lowe

## 1. Project Vision & Core Principles

**Family Lowe** is an open-source, GitHub-hosted, automated publication pipeline that compiles personalized, finite, daily and weekly digital digests into e-reader-optimized EPUB files and multi-target artifacts.

Designed for e-readers (such as Kobo), mobile devices, and desktop reading environments, **Family Lowe** transforms fragmented RSS feeds and web articles into structured, finishable personal editions. It organizes content into distinct family member sections (`Dejvid`, `Anna`, `Nea`, `Alice`), enforces strict reading budgets, tracks weekly trends, and provides optional AI-assisted editorial context without altering original journalistic prose.

### Core Architectural Guarantees

1. **Journalistic Integrity & Zero Content Rewriting:** Original articles are preserved verbatim (minus web clutter/ads). The LLM is never permitted to "play journalist," synthesize fake text, or rewrite original journalistic pieces. All AI outputs (summaries, weekly context intros, term explainers) appear strictly as separate, explicitly labeled sidebars, section ingresses, or editor notes with direct links back to original backing sources.
2. **LLM as an Optional Enhancement:** The core pipeline (ingestion, cleaning, deduplication, budget filtering, compilation, publishing) is 100% functional without an LLM. When enabled, the LLM layer adds value through metadata extraction, section ingresses, concept explainers, and trend tracking.
3. **Finishability First:** Every issue enforces hard word and article budgets. The daily edition is designed to be read in 10–15 minutes, while the weekly edition provides a 45-minute deep dive.
4. **Modular & Testable Architecture:** Every component (ingestors, deduplicators, LLM providers, publishers) implements a strict, typed Interface/Abstract Base Class for local development, easy unit testing, and component swapping.

---

## 2. System Architecture & Detailed Component Design

```text
                                  +---------------------------------------+
                                  |         Sources & RSS Feeds           |
                                  +---------------------------------------+
                                                      |
                                                      v
                                  +---------------------------------------+
                                  |    2.1 Ingestion & Health Discovery   |
                                  |       (trafilatura / Auto-Fallback)   |
                                  +---------------------------------------+
                                                      |
                                                      v
                                  +---------------------------------------+
                                  | 2.2 Deterministic + LSH Deduplication |
                                  |    (Exact -> MinHash -> Pairing)      |
                                  +---------------------------------------+
                                                      |
                                                      v
                                  +---------------------------------------+
                                  | 2.3 & 2.4 State & Trend Engine        |
                                  |    (SQLite: SQLite / Pydantic Schema) |
                                  +---------------------------------------+
                                                      |
                                                      v
                                  +---------------------------------------+
                                  | 2.5 & 2.6 Budgeting & Ingress Layer   |
                                  | (Tiering + Unmodified Text + Ingress) |
                                  +---------------------------------------+
                                                      |
                                                      v
                                  +---------------------------------------+
                                  | 2.5 EPUB Compiler (Pandoc / Calibre)  |
                                  +---------------------------------------+
                                                      |
                                                      v
                                  +---------------------------------------+
                                  | 2.7 Pluggable Publisher Pipeline      |
                                  | (GDrive/GitHub Artifacts/Releases/etc)|
                                  +---------------------------------------+

```

---

### 2.1 Ingestion & Smart Fallback Engine

* **Content Extraction:** Uses `trafilatura` and `readability-lxml` to extract raw body text and images while stripping ads, navigation bars, tracking scripts, and cookie banners.
* **Pre-Ingestion Health Check:** Validates source availability before execution.
* **Programmatic Fallback Discovery:**
* If an RSS feed returns HTTP 4xx/5xx or empty payloads, the engine initiates a programmatic fallback search across alternative feed URLs or sitemap endpoints under the publisher's domain.
* If a source fails completely, the pipeline logs a non-fatal warning and dynamically redistributes the word budget to active sources in that section.



---

### 2.2 Deduplication & Multi-Perspective Contextualization

To handle stories covered across multiple publications without blowing content budgets or rewriting articles:

1. **Stage 1 (Exact Match - Deterministic):** Canonical URL hashing and SHA-256 body text hashing eliminate identical duplicates instantly.
2. **Stage 2 (Fuzzy Match - Locality-Sensitive Hashing):** MinHash / LSH shingling (`datasketch`) identifies articles with high lexical overlap across different publishers.
3. **Stage 3 (Contextual Ingress - Non-Intrusive LLM Layer):**
* Instead of merging or altering the original texts, the pipeline groups the original, unmodified articles under a unified story header.
* If the LLM layer is enabled, it generates a brief **Comparative Editorial Note** prepended to the group (e.g., *🤖 Editor's Note: "The following two articles cover the recent policy release. Source A focuses on economic impacts, while Source B details regulatory timelines."*). Both original articles remain 100% intact below the note with clear attribution links.



---

### 2.3 Edition Engine: Daily "Espresso" vs. Weekly "Deep Dive"

* **Daily "Morning Espresso" Edition:**
* **Target Duration:** 10–15 minutes (~1,500–2,500 words).
* **Structure:** Focused, high-priority updates, bulleted section ingresses, and top 2–3 items per active section.


* **Weekly "Sunday Deep Dive" Edition:**
* **Target Duration:** 45–60 minutes (~8,000–12,000 words).
* **State Integration:** Queries the SQLite database for topics that recurred across daily editions during the week.
* **Weekly Evolution Overview:** Prepends an LLM-generated weekly summary explaining how major ongoing stories evolved over the past 7 days, explicitly citing and linking to the underlying articles included in the weekly issue.



---

### 2.4 Strongly Typed State Management & Schema Specification

State persistence is maintained via a lightweight SQLite database (`state.db`) using SQLAlchemy ORM / Pydantic models. Database migrations are handled via Alembic. All entities follow strict best-practice metadata schemas (UUID primary keys, explicit UTC timestamps).

#### Database Schema Definition (`src/family_lowe/state/models.py`)

```python
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Integer, DateTime, Text, Float, ForeignKey, JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

class Base(DeclarativeBase):
    pass

class ArticleState(Base):
    """Tracks seen articles to prevent duplicates and calculate trend metrics."""
    __tablename__ = "articles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str] = mapped_column(String(100), nullable=False)
    section_id: Mapped[str] = mapped_column(String(50), nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Metadata and Tracking
    appearance_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_edition_type: Mapped[str] = mapped_column(String(20), nullable=False) # 'daily' or 'weekly'

    # Audit Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default_utc_now, onupdate=utc_now)

class TopicTrend(Base):
    """Aggregates topic frequency over rolling 7-day windows for weekly deep dives."""
    __tablename__ = "topic_trends"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    keyword_cluster: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    frequency_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    associated_article_ids: Mapped[dict] = mapped_column(JSON, nullable=False, default=list) # List of UUIDs

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, defaultutc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

```

---

### 2.5 Personalization, Section Ingresses & Non-Intrusive "Explainers"

* **Section Ingress Synthesizer:** Every section (`Dejvid`, `Anna`, `Nea`, `Alice`) opens with an optional LLM-generated conversational overview introducing the section's contents (e.g., *"Welcome to today's Tech section. Today we feature three pieces on cloud architecture, including new Kubernetes releases."*).
* **Non-Intrusive "Explainers" (Sidebar/Callout Box):**
* When educational or age-specific adaptations are enabled (`explain_mode: true`), the original text is **never rewritten**.
* Instead, the LLM generates a distinct **"Key Concepts & Glossary"** callout box inserted directly above or beside the original article text. This provides background context or simplified definitions for younger readers while preserving the authentic journalistic source material.



---

### 2.6 Content Budgeting & Priority Tiering

To guarantee finishability, every compiled issue adheres to priority tiers:

| Priority Tier | Selection Criteria | Layout Placement |
| --- | --- | --- |
| **Tier 1: Must Reads** | High trend score, explicit user focus area, or breaking news. | Prominently featured at the top of the section with full, clean original text and optional LLM intro. |
| **Tier 2: Nice to Read** | Secondary stories fitting remaining section word budget. | Middle of section with full original text. |
| **Tier 3: Bonus Reads Index** | Articles fetched that exceed the strict word budget. | Compiled into a "Bonus Reads" index page at the end of the EPUB featuring original headline, 2-line RSS description, and original URL. |

---

### 2.7 Pluggable Publisher Framework

The pipeline decouples format compilation from delivery. Publishers extend `BasePublisher`:

```python
from abc import ABC, abstractmethod
from pathlib import Path

class BasePublisher(ABC):
    """Abstract Base Class for all Family Lowe output drivers."""

    @abstractmethod
    def publish(self, epub_path: Path, metadata: dict) -> bool:
        """Publish the compiled EPUB artifact to the destination target."""
        pass

```

#### Supported Drivers

1. **`GDrivePublisher`:** Uploads EPUBs to a target Google Drive folder via Google Cloud Service Account API.
2. **`GitHubArtifactPublisher`:** Saves the generated EPUB as a downloadable workflow artifact in GitHub Actions.
3. **`GitHubReleasePublisher`:** Attaches compiled Weekly / Daily issues to GitHub Releases under automated version tags (e.g., `v2026.08.08-daily`).
4. **`CalibreWebPublisher`:** Pushes files directly to a self-hosted Calibre-Web REST API.
5. **`EmailPublisher`:** Delivers EPUBs via SMTP to e-reader email endpoints.
6. **`LocalPublisher`:** Saves files to a designated local directory for offline development and testing.

---

## 3. Self-Documenting Configuration (`config.yaml`)

The system configuration is fully commented, human-readable, and strongly validated via Pydantic at runtime.

```yaml
# ==============================================================================
# FAMILY LOWE CONFIGURATION FILE
# ==============================================================================
# This file controls the schedule, sources, section layouts, LLM providers,
# and distribution targets for the Family Lowe publication pipeline.
# ==============================================================================

version: "1.0"

publication:
  title: "Family Lowe Digest"
  publisher: "Lowe Publishing"
  language: "sv" # Primary language for UI labels and section titles ('sv' or 'en')

# ------------------------------------------------------------------------------
# EDITION TARGET BUDGETS
# Controls max length to ensure the digest remains finishable in one sitting.
# ------------------------------------------------------------------------------
editions:
  daily:
    target_words: 2500            # Total word budget for daily issue
    max_articles_per_section: 3   # Upper limit of articles per section
  weekly:
    target_words: 10000           # Total word budget for weekly issue
    max_articles_per_section: 8   # Upper limit of articles per section

# ------------------------------------------------------------------------------
# OPTIONAL LLM PROVIDER CONFIGURATION
# Set 'enabled: false' to run the pipeline in 100% deterministic mode.
# Supported providers: 'ollama', 'openai', 'anthropic', 'gemini', 'openrouter'
# ------------------------------------------------------------------------------
llm:
  enabled: true
  provider: "ollama"               # Default provider for local development
  model: "gemma4:12b"              # Local Ollama model (e.g. gemma4:12b, gemma4:4b)
  fallback_provider: "openai"     # Optional fallback if primary fails
  fallback_model: "gpt-4o-mini"
  temperature: 0.2

# ------------------------------------------------------------------------------
# SECTION DEFINITIONS & SOURCES
# Define individual family member sections, active sources, and priorities.
# ------------------------------------------------------------------------------
sections:
  - id: "dejvid"
    title: "Dejvid: Tech, Infrastructure & Science"
    enabled: true
    schedule: "both"                # Options: 'daily', 'weekly', or 'both'
    target_words: 1000
    ingress_summary: true          # Generate an LLM section overview ingress
    explain_mode: false            # Do not add concept explainer sidebars
    sources:
      - name: "Ars Technica"
        url: "https://feeds.arstechnica.com/arstechnica/index"
        type: "rss"
        priority: 1
      - name: "SVT Nyheter Vetenskap"
        url: "https://www.svt.se/nyheter/vetenskap/rss.xml"
        type: "rss"
        priority: 2

  - id: "anna"
    title: "Anna: Culture & Current Affairs"
    enabled: true
    schedule: "both"
    target_words: 1000
    ingress_summary: true
    explain_mode: false
    sources:
      - name: "SVT Nyheter Kultur"
        url: "https://www.svt.se/nyheter/kultur/rss.xml"
        type: "rss"

  - id: "nea"
    title: "Nea's Corner"
    enabled: true
    schedule: "weekly"             # Appears only in the Sunday edition
    target_words: 800
    ingress_summary: true
    explain_mode: true             # Generate background explainer sidebars for complex terms
    sources: []

  - id: "alice"
    title: "Alice's Corner"
    enabled: true
    schedule: "weekly"
    target_words: 800
    ingress_summary: true
    explain_mode: true
    sources: []

# ------------------------------------------------------------------------------
# PUBLISHING TARGETS
# Configure output destinations. Multiple destinations can be active.
# ------------------------------------------------------------------------------
publishers:
  local:
    enabled: true
    output_dir: "./output"

  github_artifact:
    enabled: true                  # Saves as downloadable workflow artifact in CI

  github_release:
    enabled: false                 # Creates an automated release tag on GitHub
    repository: "family-lowe/digest"

  gdrive:
    enabled: true
    folder_id_env: "GDRIVE_FOLDER_ID"  # Read from GitHub Secrets / Env

```

---

## 4. LLM Provider Abstraction & Multi-Provider Testing Strategy

The LLM abstraction layer uses the Unified Provider Pattern, allowing seamless swapping between local open-weight models and cloud API providers.

```python
from abc import ABC, abstractmethod
from pydantic import BaseModel

class LLMResponse(BaseModel):
    content: str
    model_used: str
    prompt_tokens: int = 0
    completion_tokens: int = 0

class BaseLLMProvider(ABC):
    """Abstract Interface for LLM execution."""

    @abstractmethod
    def generate_text(self, prompt: str, system_prompt: str | None = None) -> LLMResponse:
        pass

```

### Supported Providers

1. **Ollama Driver (`ollama`):** Local execution targeting Google's **Gemma 4** (`gemma4:12b` / `gemma4:4b`) or other open-weight models (`qwen2.5`).
2. **OpenAI Driver (`openai`):** Integration with `gpt-4o` and `gpt-4o-mini`.
3. **Anthropic Driver (`anthropic`):** Integration with `claude-3-5-sonnet` and `claude-3-5-haiku`.
4. **Google Gemini Driver (`gemini`):** Integration with `gemini-1.5-pro` and `gemini-1.5-flash`.

### Testing & Mocking Strategy (`pytest` + `vCR`)

* **Deterministic Core Tests:** All parser, extractor, deduplicator, and EPUB compiler tests run without making any external network or LLM calls.
* **LLM Integration Tests:** Network calls to LLM APIs (Ollama, OpenAI, Anthropic, Gemini) are recorded using `pytest-recording` (VCR cassettes) in `tests/fixtures/cassettes/`.
* **Multi-Provider Test Matrix:**
* CI executes unit tests against recorded VCR cassettes for all 4 providers.
* Local development defaults to `Ollama` with `gemma4` running on `http://localhost:11434`.



---

## 5. Developer Experience, DevEx & Tooling

* **Language & Runtime:** Python 3.12+
* **Package & Environment Management:** `uv`
* **Formatting & Linting:** `ruff`
* **Static Type Checking:** `mypy` (strict mode: `disallow_untyped_defs = true`)
* **Test Runner:** `pytest` with coverage reporting (`pytest-cov`)
* **DevContainer Support:** Pre-configured `.devcontainer/devcontainer.json` featuring Python 3.12, `uv`, `pandoc`, `calibre`, and `ollama` for immediate, zero-setup containerized development.
* **Continuous Maintenance:** `.github/dependabot.yml` monitors and opens weekly PRs for Python dependencies (`uv.lock`), GitHub Actions, and DevContainer base images.

---

## 6. Milestone Implementation Roadmap

* [ ] **Milestone 1: Core Foundation & Tooling**
* Repository initialization with `uv`, `ruff`, `mypy`, `pytest`, and `.devcontainer`.
* Set up `.github/dependabot.yml` and basic CI pipeline (`ci.yml`).
* Implement Pydantic `config.yaml` parser and typed error handling.


* [ ] **Milestone 2: Ingestion, Extraction & Local Deduplication**
* Implement `trafilatura` extraction engine with HTML cleaning.
* Implement pre-ingestion health checks and programmatic fallback discovery.
* Implement MinHash / LSH deterministic deduplication layer.


* [ ] **Milestone 3: Typed State Management & Database Migrations**
* Implement SQLAlchemy / Pydantic `state.db` models (`ArticleState`, `TopicTrend`) with `created_at` / `updated_at` timestamps.
* Set up Alembic migration scripts and GitHub Actions cache persistence.


* [ ] **Milestone 4: LLM Abstraction & Multi-Provider Integration**
* Implement `BaseLLMProvider` interface.
* Implement drivers for `Ollama` (Gemma 4), `OpenAI`, `Anthropic`, and `Gemini`.
* Create non-intrusive section ingress and explainer sidebar generators.
* Record VCR test cassettes for all providers.


* [ ] **Milestone 5: EPUB Compilation & Pluggable Publishers**
* Build Markdown-to-EPUB compiler using Pandoc and Kobo-optimized CSS (`styles/kobo.css`).
* Implement `BasePublisher` drivers: `LocalPublisher`, `GDrivePublisher`, `GitHubArtifactPublisher`, and `GitHubReleasePublisher`.


* [ ] **Milestone 6: GitHub Actions Workflows & E2E Validation**
* Implement `.github/workflows/generate_daily.yml` and `generate_weekly.yml`.
* Verify end-to-end publishing to Google Drive and GitHub Releases.
* Validate rendering on Kobo e-reader and mobile apps.
