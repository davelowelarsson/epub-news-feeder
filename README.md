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

## Remote verified summaries

`llm_processing: remote_allowed` and `eligibility.remote_llm: allow` are two separate gates and
both must say yes before an Article leaves the machine — the publisher's recorded position and the
operator's policy are different questions. `conditional` and `unknown` refuse: silence is not
permission. Today exactly one Source passes both, the operator's own site.

The provider profile is a load-time gate, not a runtime one. A profile declaring `training_opt_in`
or `store` refuses to load the configuration at all, before a Source is fetched, because there is
no correcting it once text has been sent. Requests set `store: false`, `tools: []`, and
`prompt_cache_retention: in_memory` — the default is a 24-hour prompt cache, and with `store` off
that cache is the only remaining server-side copy.

Model Pairs are pinned to dated snapshots. A floating alias would change the Edition under a
schedule with nobody reading the diff.

The Edition's end matter states which route produced its summaries and names every Source left out
of it, so a reader can tell a policy exclusion from a failure.

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

## The scheduled Editions

`.github/workflows/daily-edition.yml` builds one Edition every weekday at 03:17 UTC — 05:17 in
Stockholm through the summer, 04:17 through the winter, since GitHub cron does not observe DST.
The requirement is 07:00 Stockholm, so that is a deliberately early start: a scheduled run is a
request rather than a promise, and GitHub queues it for as long as it likes. Scheduling on the
hour cost 67 to 96 minutes of queue on three consecutive weekdays, against a job that finishes
in under five. Hence the odd minute and the wide margin — see the comment in the workflow.
It runs the `daily` Publication of `examples/reality-check.yaml`. A hosted runner has no Ollama,
so its summaries come from the remote editorial route — which reaches exactly one Source, the one
whose rightsholder granted it.

`.github/workflows/weekly-edition.yml` carries Saturday, at 03:11 UTC and by the same arithmetic.
Sunday has none. It runs the `weekly` Publication: the same Sources and Sections as the daily at
twice the Budget, and — this is the whole of what makes it a weekly rather than a sixth daily —
`reads_history_from: [daily]`.

Suppression is otherwise per-Publication, deliberately, so a Saturday Edition built from the same
feeds would carry precisely the reading the weekdays had already delivered. That reference is the
one sanctioned way through the boundary: every Article *and every Brief* the daily delivered is
suppressed, and the Story Clusters it kept returning to rank ahead of Source weight in each
Section's ordering, so Saturday completes the week rather than reprinting it. The reference is
one-directional — the daily is unaware the weekly exists, and no weekday Edition changes because
the weekly is configured.

Recurrence is read from `deliveries`, not from `cluster_coverage`. Coverage exists to render a
Story Hub and is incomplete in three ways that do not matter for display and matter entirely for
ordering: it keeps an Article's first delivery date forever, the spool-resume path never writes
it, and Articles from Sources whose feeds carry no publication date are skipped. It ranks below a
reader's declared interest and never reorders the Essential Coverage Slice.

One limit worth knowing: a Saturday Run acquires from the same feeds as any other, so it carries
only what is still in them. Monday's near-miss has usually fallen off by Saturday, and re-fetching
one by its stored `canonical_url` needs an acquisition route that does not exist yet.

A hosted runner also starts with an empty disk, so the State Store is restored from Google Drive
before each run and saved back after delivery. Without it every morning would re-deliver the
same reading. Runs queue rather than overlap — both workflows share `concurrency: edition`, never
cancelled: a cancelled run can deliver an Edition whose state was never saved.

Six repository secrets, named identically to the local `.env` keys — no mapping layer:

| Secret | Purpose |
| --- | --- |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN` | Desktop OAuth client, `drive.file` scope only |
| `GOOGLE_DRIVE_FOLDER_ID` | Where Editions land, inside the folder that syncs to the device |
| `GOOGLE_DRIVE_FOLDER_DB` | The State Store archive, outside the synced tree |
| `OPENAI_API_KEY` | The remote editorial route |

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
remains the outstanding milestone.

## Prototypes

`prototypes/` is an untracked scratch area for trying an idea out — a state model, a rendering
shape, a throwaway script — without it having to survive review or the quality gate. Nothing there
is imported by `src/` or `tests/`. When a prototype settles a decision, record the decision on its
issue and delete the prototype.
