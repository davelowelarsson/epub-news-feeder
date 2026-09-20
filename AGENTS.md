# Working agreements

Repo-specific rules for anyone, human or agent, working in this codebase. Global conventions
live in the contributor's own configuration; this file records only what is true *here* and
what has already cost someone a day.

## Domain language

`CONTEXT.md` is the glossary and is authoritative. Use its terms exactly — an Edition is not a
build, a Delivery Copy is not a backup, a Device Copy is not a Delivery Copy. Keep `CONTEXT.md`
free of implementation detail; it is a glossary, not a spec.

## Running the checks

The full suite needs the console script on `PATH`, because ~22 CLI tests invoke
`epub-news-feeder` as a subprocess:

```
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q
```

Without it those tests fail with `FileNotFoundError: 'epub-news-feeder'` and look exactly like
a regression you just caused. EPUBCheck-marked tests additionally need the reviewed 5.3.0 jar
at `.local/tools/epubcheck-5.3.0/epubcheck.jar` or `EPUBCHECK_JAR`; they fail closed, so never
report a green suite while they are failing.

The gate is `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, and the suite.

## Editions are deterministic

Identical inputs must produce identical bytes. `tests/test_epub_delivery.py` pins a SHA-256 of a
built Edition for exactly this reason. When that digest changes, confirm the change was intended
and that a semantic assertion covers it — then update it deliberately. Never update it to make a
red test green.

## Never dispatch a Publication to test it

A `workflow_dispatch` run records deliveries under that `publication_id`, and its own history
then suppresses those Articles from the next scheduled Edition — potentially below
`min_articles`, which fails the run. Deleting the delivered file from Drive does not undo this;
the damage is in the State Store.

Test against real history instead: `state-pull` into a local copy, copy that to a trial
database, then `generate` with `GOOGLE_DRIVE_FOLDER_ID` and `GOOGLE_DRIVE_FOLDER_DB` **unset**,
which makes delivery and state-push impossible rather than merely unlikely.

`.env` at the repo root holds the Drive and OpenAI credentials. It is gitignored, so it does not
exist inside worktrees — read it by absolute path rather than concluding the credentials are
unavailable.

## Kobo: blank pages are never a markup fault

This has been misdiagnosed twice. Before changing `epub.py`, read the first four bytes of the
file on the device: `PK\x03\x04` is healthy, `{` is a Google error body the device prepended.
Run `epub-news-feeder kobo-repair --volume /Volumes/KOBOeReader` — it reports without writing.

See [docs/kobo-drive-delivery.md](docs/kobo-drive-delivery.md) for the full diagnosis. The short
version: a book that opens with a working cover and table of contents but blank pages is a
damaged container, not markup. Desktop readers show the same file perfectly, which is what makes
it look like a rendering bug.

Do not propose a legacy `toc.ncx` or a `.kepub.epub` Delivery Copy. Both were declined on
2026-09-06: NCX is deprecated in EPUB 3.3 and Kobo documents that it ignores one anyway, and
kepub locks the output to one vendor. Fix Kobo problems inside the standard first.

## Touching a connected device

The Kobo is someone's library and mounts as FAT. Copy `KoboReader.sqlite` elsewhere before
querying it and never write to it. Any tool that writes to the device must verify against the
source of truth first, back up before it mutates, and replace atomically — see
`src/epub_news_feeder/kobo.py`.

## Claims

State what was verified and how. "Every download is corrupted" and "this halves your exposure"
were both asserted here on the strength of a pattern that later turned out to be an artefact of
how the evidence was collected. A hypothesis worth testing is not a finding, and neither is a
plausible mechanism. If something has not been observed, say so.
