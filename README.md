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

## The scheduled Edition

`.github/workflows/daily-edition.yml` builds one Edition every day at 04:00 UTC — 06:00 in
Stockholm through the summer, 05:00 through the winter, since GitHub cron does not observe DST.
It runs the `daily` Publication of `examples/reality-check.yaml`: the deterministic core, with no
LLM call at all, because a hosted runner has no Ollama.

A hosted runner also starts with an empty disk, so the State Store is restored from Google Drive
before the run and saved back after delivery. Without it every morning would re-deliver the
same reading. Runs queue rather than overlap (`concurrency: edition`, never cancelled): a
cancelled run can deliver an Edition whose state was never saved.

Six repository secrets, named identically to the local `.env` keys — no mapping layer:

| Secret | Purpose |
| --- | --- |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN` | Desktop OAuth client, `drive.file` scope only |
| `GOOGLE_DRIVE_FOLDER_ID` | Where Editions land, inside the folder that syncs to the device |
| `GOOGLE_DRIVE_FOLDER_DB` | The State Store archive, outside the synced tree |
| `OPENAI_API_KEY` | Reserved for remote editorial; nothing reads it yet |

Delivery Copies are named `<date>-<publication>-<run>.epub`, date first, because a reader that
truncates a long filename truncates it from the right.

Run it by hand from the Actions tab (`workflow_dispatch`) before trusting the schedule.

## Quality gate

```bash
uv sync --frozen --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

CI runs this exact gate — `.github/workflows/quality-gate.yml` executes the same four commands
against a verified EPUBCheck 5.3.0, so the local gate and CI cannot drift.

Implementation follows the project specification in GitHub issue #16. Physical Kobo acceptance
and OpenAI editorial integration remain subsequent milestones.

## Prototypes

`prototypes/` is an untracked scratch area for trying an idea out — a state model, a rendering
shape, a throwaway script — without it having to survive review or the quality gate. Nothing there
is imported by `src/` or `tests/`. When a prototype settles a decision, record the decision on its
issue and delete the prototype.
