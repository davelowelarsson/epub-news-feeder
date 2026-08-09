# EPUB News Feeder — Implementation Handoff

## Status

**Working local MVP implemented; upgraded reader experience and local verified-summary path passed
automated and live acceptance on 2026-08-09. Human/device acceptance remains pending.**

The repository has progressed through `/to-spec`, `/to-tickets`, and the automated local
`/implement` slice. GitHub issue #16 is the authoritative specification; issues #17–#29 define
the implementation tickets. The next implementation session should start with private state and
Google Drive delivery, then test the OpenAI adapter. Do not weaken the deterministic core or
Source-specific processing rules while adding either.

## Working local surface

- Locked CPython 3.13 `uv` project with installed `epub-news-feeder` CLI.
- Strict version-1 YAML validation before network/state/delivery effects.
- Dated, independent Source route gates; fail-closed robots/access/origin handling.
- Configured full-text feed/page acquisition, metadata-only Ekot, bounded downloads, safe
  degradation, fail-closed robots redirects, and connected-peer address validation.
- Private SQLite identity, provenance, keyed fingerprints, revision/correction/cluster history,
  writer lock, reservations, Pending Delivery, and acknowledged delivery lifecycle.
- Coverage/Interest selection with Essential Coverage, configurable plurality, Discovery, Mutes,
  leaf minima, ancestor ceilings, cluster diversity, and one-body multi-Section placement.
- Deterministic EPUB 3.3 with an Edition overview, article-level nested navigation, explicit
  author/date fallbacks, notes, corrections, Story hubs, reciprocal links, attribution, update
  metadata, and a body-free colophon Run ID.
- Metadata-only reports compete in the same finite selection budget and appear as attributed,
  navigable publisher-link briefs. Unselected acquisition inventory is never rendered or stored.
- Digest-pinned EPUBCheck 5.3.0 gating, immutable private local delivery, and revalidated
  same-Run recovery from a private pending spool.
- Body-free allowlisted JSONL diagnostics with private permissions and retention.
- Optional local Ollama summaries using separate `gemma4:12b-mlx` editor and `gemma4:e4b-mlx`
  verifier roles, strict cited schemas, one repair maximum, and deterministic omission on failure.

## Reproduce locally

```bash
uv sync --frozen --all-groups
uv run epub-news-feeder validate --config examples/reality-check.yaml
mkdir -p .local/editions
uv run epub-news-feeder generate \
  --config examples/reality-check.yaml \
  --state .local/state.sqlite3 \
  --output .local/editions \
  --diagnostics .local/diagnostics
uv run epub-news-feeder ollama-check --model gemma4:12b-mlx
uv run epub-news-feeder ollama-check --model gemma4:e4b-mlx
```

The default local EPUBCheck path is
`.local/tools/epubcheck-5.3.0/epubcheck.jar`; `EPUBCHECK_JAR` may point to the exact reviewed
binary. Preserve the state database and its adjacent `.key` file together. Losing the key
removes comparability of privacy-preserving revision fingerprints.

Quality gate:

```bash
uv sync --frozen --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

Private live evidence is recorded under ignored `.local/`; never commit Editions, state,
detailed diagnostics, Run IDs, delivery identifiers, or digests.

An interrupted final delivery is resumed with the original `--run-id` and `--at`. The private
spool sits beside the State Store under `pending-editions/` until State finalization succeeds.

## Next implementation session

1. Add external private state persistence and lease/conditional-generation semantics. Store and
   restore the SQLite database plus fingerprint key as one integrity-checked private unit.
2. Add narrow Google user OAuth `drive.file` delivery into the preselected `Rakuten Kobo`
   folder. Reconcile by pre-generated file ID and Pending Delivery; Drive remains a target,
   never the State Store.
3. Record a human Calibre visual review and physical Kobo Libra Colour acceptance for the exact
   private digest. Automated EPUBCheck and Calibre parsing already pass but do not replace those
   human/device checks.
4. Complete the remaining editorial qualification: hard sentence/word/language ceilings,
   worst-case call/token reservation, adversarial evals, then OpenAI Responses with `store: false`
   and no tools. Local Ollama article summaries are already connected and fail closed.

## Safeguards

- Full Article bodies exist only in transient memory and private EPUB snapshots—not state,
  diagnostics, public logs, tests, or tracked artifacts.
- Ekot stays metadata/link-only and never enters an LLM prompt.
- David Lowe Larsson and Ars Technica remain local-only for LLM processing. SVT remote use needs
  explicit Publication opt-in plus a current compatible provider preflight.
- Eligibility evidence is dated operational policy, not legal advice. Re-review it before expiry
  or any changed terms, robots signal, origin, model, provider, audience, or use.
- Full-body eligibility is an explicit Source-route assertion plus conservative size/word gates;
  it does not infer publisher completeness. Re-review each route when its format changes.
- Preserve user-owned `.vscode/` and `prototypes/` files.
- Google Drive/OpenAI/Kobo work is not part of the completed local delivery path.
