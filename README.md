# EPUB News Feeder

Build private, finite news Editions as standards-first EPUBs. The local MVP validates
strict YAML, applies dated Source eligibility, acquires Articles from configured full-text routes,
selects a finite Edition, optionally adds locally verified summaries, gates a deterministic EPUB
3.3 with EPUBCheck, and atomically
delivers it while retaining only body-free SQLite state and JSONL diagnostics.

## Local setup

Requirements: `uv`, Java 17+, and EPUBCheck 5.3.0. The expected `epubcheck.jar`
SHA-256 is `f7f96617c929371821609b88c8484d6dc9f24fe916499863c46094c5fb778a65`.
Calibre is optional for local reader inspection (`brew install --cask calibre` on macOS).

```bash
uv sync --frozen --all-groups
export EPUBCHECK_JAR=/absolute/path/to/epubcheck-5.3.0/epubcheck.jar
uv run epub-news-feeder validate --config examples/reality-check.yaml
mkdir -p .local/editions
uv run epub-news-feeder generate \
  --config examples/reality-check.yaml \
  --state .local/state.sqlite3 \
  --output .local/editions \
  --diagnostics .local/diagnostics
```

Eligibility evidence is dated operational policy, not legal advice. Re-review it before
expiry or any route/provider change. Generated Editions are private single-operator copies.
Back up the SQLite file and its adjacent `.key` sidecar together.
If final delivery is interrupted, rerun with the same `--run-id` and `--at`; the validated
private spool is revalidated and delivered without reacquiring Sources.

## Local verified summaries

The example configuration uses `gemma4:12b-mlx` as editor and `gemma4:e4b-mlx` as an independent
verifier. Both must be installed in Ollama. Every sentence requires a publisher citation; malformed,
unsupported, unavailable, or policy-ineligible output is omitted without blocking the Edition.
English and Swedish Articles are processed in separate language batches and pass a deterministic
language check before verifier acceptance. Generated text is visibly separated from publisher text,
with one method note in Edition end matter. Metadata-only sources such as Ekot are never sent to a
model.

```bash
uv run epub-news-feeder ollama-check --model gemma4:12b-mlx
uv run epub-news-feeder ollama-check --model gemma4:e4b-mlx
```

Set `editorial.enabled: false` for the fully deterministic no-LLM path.

## Quality gate

```bash
uv sync --frozen --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

Implementation follows the project specification in GitHub issue #16. Google Drive, scheduled
private state, physical Kobo acceptance, and OpenAI editorial integration remain subsequent
milestones.
