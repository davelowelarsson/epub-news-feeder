# EPUB News Feeder — Wayfinder to Specification Handoff

## Purpose

Start a fresh agent session at the end of planning, convert the settled Wayfinder map into an implementation specification, decompose that specification into executable tickets, then implement test-first.

## Readiness

**Status: READY for `/to-spec`.** Map completed 2026-08-09.

All readiness checks pass:

1. [Source acquisition and LLM processing eligibility](https://github.com/davelowelarsson/epub-news-feeder/issues/15) is resolved and closed.
2. The [Wayfinder map](https://github.com/davelowelarsson/epub-news-feeder/issues/1) has 14 closed child decisions and no remaining in-scope fog.
3. The handoff commit is pushed to `main`; the commands below verify synchronization before `/to-spec` begins.

## Authoritative planning artifacts

- [Wayfinder map](https://github.com/davelowelarsson/epub-news-feeder/issues/1) — index of every settled decision. Resolution details live in each linked ticket comment.
- [Domain language](./CONTEXT.md) — canonical terminology; implementation details do not belong here.
- [Initial discussion](./initial-discussion.md) — origin and examples only. When it conflicts with a later ticket resolution, the ticket wins.
- [LLM editor prototype](https://github.com/davelowelarsson/epub-news-feeder/tree/prototype/issue-14-llm-editor) — throwaway primary-source artifact, not production code and not intended for merge.
- [Source eligibility research](https://github.com/davelowelarsson/epub-news-feeder/blob/research/issue-15-source-eligibility/docs/research/0015-source-eligibility.md) — dated evidence and acceptance checks for acquisition, LLM processing, retention, and private distribution.
- Research reports live on their linked `research/*` branches. Their ticket resolution comments are authoritative.

## Non-negotiable route

- Build a configurable, general-purpose, open-source Python application. Personal/family configuration is an instance of the product, not its domain model.
- The deterministic core must ingest, select, deduplicate, package, validate, and deliver Editions without an agent framework or LLM.
- Feed excerpts are discovery metadata, never a substitute for complete journalism. Use verified full feed content or extract the publisher page; omit failed Articles with minimal reader notes and private diagnostics.
- Use strict versioned YAML, reusable Sources and Policy Presets, ordered nested Sections, inherited policies/Budgets, and pre-fetch validation.
- Use private environment-isolated SQLite state, canonical Article identity, Content Revision hashes, per-Publication delivery history, reservations, and conservative deduplication/clustering.
- Preserve complete attributed Articles. Never rewrite, merge, invent, or publicly distribute publisher journalism.
- Generate a standards-first non-DRM EPUB 3.3 with a deterministic project-owned Python writer; gate with pinned EPUBCheck, inspect locally in Calibre, and physically accept on Kobo Libra Colour. KEPUB is optional and derived.
- First remote delivery is an immutable EPUB in Google Drive's `Rakuten Kobo` folder using narrow user OAuth and an idempotent Pending Delivery. Google Drive is not the State Store.
- Public repository and GitHub Actions workflows contain no private Editions, state, detailed diagnostics, credentials, or delivery identifiers. Public logs are allowlisted aggregates only.
- One failed Source or optional LLM enhancement does not block an otherwise publishable Edition. Only the Publication minimum, EPUB validation, or failed required delivery blocks completion.
- The early optional LLM editor uses strict structured proposals, separate pinned Editorial/Verifier Models, sentence citations, independent verification, at most one repair plus re-verification, bounded cost/privacy, measurable influence, and deterministic fallback.
- Source eligibility is independently gated. David Lowe Larsson and Ars Technica default to local-only LLM processing; SVT remote processing requires explicit Publication opt-in and provider preflight; Ekot remains metadata/link-only and excluded from LLM processing. Unknown permission never becomes allow.

## Specification sequence

The fresh session should run:

1. `/to-spec` using the Wayfinder map as the decision source and this file as the handoff.
2. Review the generated specification against every closed map ticket; cite the ticket title/link beside each requirement or acceptance criterion.
3. `/to-tickets` from the reviewed specification. Preserve dependency order and define testable acceptance commands per ticket.
4. Implement with TDD (`red → green → refactor`) in vertical slices.

Recommended delivery slices, without changing settled scope:

1. **Local reality check:** config validation; real feed discovery/acquisition; Source health; identity/state; exact/content deduplication; deterministic selection/Budgets; EPUB construction; EPUBCheck and Calibre acceptance.
2. **Private delivery:** reservations; diagnostics; scheduled public-repo workflow; external private state persistence; idempotent Google Drive handoff; Kobo physical acceptance.
3. **Early optional LLM:** provider adapters; structured proposal/gate; evaluation and evidence; independently releasable editorial capabilities. The deterministic core remains complete without it.

## Implementation safeguards

- Start by selecting the Python toolchain and repository skeleton in the specification; do not infer code from the obsolete illustrative snippets in `initial-discussion.md`.
- Pin runtime and validation dependencies. Pin third-party Actions by full commit SHA.
- Do not merge throwaway prototype branches into `main`; port only decisions and independently implemented production logic.
- Preserve unrelated local `.vscode/` and `prototypes/` files unless their owner explicitly scopes them into the work.
- Never store full Article bodies in SQLite or diagnostics. Private EPUBs are the retained content snapshots.
- Do not silently substitute Sources, Delivery Targets, Model Pairs, or relaxed constraints.
- Reproduce GitHub Actions failures locally using Run ID correlation before fixing them.

## Verification before starting `/to-spec`

```bash
git status --short
git diff --check
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
gh issue view 1 --json state,body,url
gh issue list --state open --label 'wayfinder:research' --json number,title,state,assignees,url
```

Expected tracked worktree: clean. Existing untracked `.vscode/` and `prototypes/` are user-owned and intentionally untouched.

## Fresh-session prompt

> Read `AGENTS.md` instructions supplied by the user, then read `HANDOFF.md` and `CONTEXT.md`. Verify the Wayfinder readiness checks and load the map plus every linked resolution comment. Run `/to-spec` without reopening settled decisions. After the specification is verified against the map, run `/to-tickets`, then begin TDD implementation in dependency order. Preserve the deterministic no-LLM core and treat optional LLM capabilities as the final early slice.
