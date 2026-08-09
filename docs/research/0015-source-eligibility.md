# Research 0015: Source acquisition and LLM processing eligibility

**Issue:** [#15 — Define Source acquisition and LLM processing eligibility](https://github.com/davelowelarsson/epub-news-feeder/issues/15)  
**Observed:** 2026-08-09  
**Fetch identity:** `epub-news-feeder research contact: https://github.com/davelowelarsson/epub-news-feeder`  
**Scope:** project policy and implementation gates, not legal advice or a representation that copyright exceptions apply in every operator's jurisdiction.

## Decision

Treat five permissions independently:

1. automated discovery/feed acquisition;
2. publisher-page acquisition;
3. local LLM processing of lawfully acquired text;
4. remote-provider LLM processing;
5. retention and private distribution in a State Store or Edition.

Passing one gate never passes another. A Source is usable only for the intersection of all gates required by the Run. Explicit terms, licences, machine-use directives, and technical access controls override defaults.

Each gate records `allow`, `deny`, `conditional`, or `unknown`. `unknown` is never treated as `allow`: unknown acquisition/retention disables that route, while unknown remote-processing permission degrades to `local_only` if acquisition, retention, and local processing independently pass.

`robots.txt` is an acquisition instruction, not a licence. [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309.html) says the Robots Exclusion Protocol is not access authorization. A missing `Disallow`, a missing robots file, or a provider-specific bot allowance therefore does not authorize copying, LLM use, retention, or distribution.

The initial Source policy is:

| Source | Feed acquisition | Publisher-page acquisition | Local LLM | Remote LLM | Private retention / attribution | Initial config result |
| --- | --- | --- | --- | --- | --- | --- |
| David Lowe Larsson | **Allow** full body from the published RSS feed. | **Allow** for validation/fallback; wildcard robots group allows `/`. | **Allow** for cited editorial use; never training/fine-tuning. | **Unknown → local-only.** The custom `use=reference` signal does not explicitly authorize disclosure to a third-party processor; named remote-provider crawlers are blocked. An operator who controls the rights may record a separate explicit grant. | **Conditional allow** for a one-user private Edition with title, author/Source, canonical URL, and unchanged publisher body. A fork's rights basis is otherwise **unknown** and must be recorded; ownership is not inferred from this repository. | `llm_processing: local_only` |
| Ars Technica | **Allow** the public feed for discovery. The feed origin returns robots 404, which clears only the REP gate. | **Allow** ordinary article paths for this project's User-Agent; do not use subscriber feeds, evade paywalls, or enter disallowed paths. | **Allow locally only** for the same private reading workflow; no training/fine-tuning. | **Unknown/negative signals → local-only.** Ars blocks GPTBot, ClaudeBot, Claude-User, Google-Extended, and many other AI agents, and offers no applicable remote-API licence. | **Allow private offline copy only** under Ars' published reprint guidance. Keep headline, byline, Ars Technica name, canonical URL, and copyright notice; never publish or share the Edition. | `llm_processing: local_only` |
| SVT Nyheter | **Allow** the public RSS feed for discovery. | **Allow** ordinary article paths: the wildcard group allows `/`. | **Allow** cited inference, not training: SVT declares `ai-input=yes` and `ai-train=no`. | **Conditional allow** only after Publication opt-in and provider preflight proves no training, no provider-side durable application state, bounded disclosed abuse retention, and no tools/subprocessors beyond the approved provider. This is a narrow interpretation of SVT's explicit real-time AI-input signal, not a redistribution licence. | **Operator-attested private use only**; the feed asserts `© Sveriges Television AB`, and no general feed licence was found. Preserve headline, byline, SVT name, canonical URL, and copyright; exclude third-party images/audio unless separately cleared. No sharing. | `llm_processing: remote_allowed` only with the stated provider and private-use gates; otherwise `local_only` |
| Sveriges Radio Ekot | **Metadata/link discovery only, transiently.** `api.sr.se/robots.txt` allows `/api/rss/` to `*`, but SR's API terms limit Material to linking/streaming and temporary copies. | **Unknown policy plus denied access → disable.** The sampled article and `www.sverigesradio.se/robots.txt` both returned 403; do not retry or bypass. | **Disable** unless SR gives prior approval: the API terms prohibit machine learning and prohibit storage, copying, preservation, and modification beyond temporary copies. | **Disable** unless SR gives prior written approval. | **No Article body in the State Store or EPUB.** A link-only attributed pointer may be retained if the implementation can guarantee that no API Material body is persisted. Audio must be linked/streamed, not downloaded. | `llm_processing: disabled`; `acquisition: metadata_only` or disable the Source until that mode exists |

These classifications are project-safe defaults, not findings that every local use is lawful. In particular, “local” avoids disclosure to an LLM provider but does not cure prohibited acquisition, copying, retention, or machine use.

## Dated evidence

### David Lowe Larsson

- [`https://davidlowelarsson.com/robots.txt`](https://davidlowelarsson.com/robots.txt) returned 200. The matching `*` group allows `/`, declares `search=yes,ai-train=no,use=reference`, and says a missing content signal neither grants nor restricts permission through that mechanism. Separate groups disallow Amazonbot, Applebot-Extended, CCBot, ClaudeBot, Google-Extended, GPTBot, and Meta's external agent.
- [`https://davidlowelarsson.com/rss.xml`](https://davidlowelarsson.com/rss.xml) returned 200 as `application/xml`, with an ETag and full `content:encoded` bodies as established by [research #2](https://github.com/davelowelarsson/epub-news-feeder/blob/research/issue-2-full-text/docs/research/0002-full-text-acquisition.md). No RSS `copyright` field or licence link was observed.
- The sampled canonical page, [`Who owns the code AI writes?`](https://davidlowelarsson.com/posts/essay-ai-code-ownership/), returned 200. Nothing in page metadata widened the robots signal into a third-party-processing or redistribution licence.

Inference: ordinary feed/page acquisition is expressly open at the REP layer. Local cited editorial use fits the operator's intended private workflow, while remote disclosure remains unproved unless the rights-controlling operator records an explicit grant.

### Ars Technica

- [`https://feeds.arstechnica.com/robots.txt`](https://feeds.arstechnica.com/robots.txt) returned 404. Under RFC 9309 this is not a robots prohibition, but it is also not authorization. The [public RSS directory](https://arstechnica.com/rss-feeds/) describes the regular feeds as a way to follow Ars in a feed reader and distinguishes subscriber full-text feeds.
- [`https://feeds.arstechnica.com/arstechnica/index`](https://feeds.arstechnica.com/arstechnica/index) returned 200 as XML with `no-cache, no-store`; it contains previews, not an explicit content licence. No RSS `copyright` or machine-readable licence field was observed.
- [`https://arstechnica.com/robots.txt`](https://arstechnica.com/robots.txt) returned 200. The matching `*` group does not disallow ordinary article paths, but Ars separately disallows a long list of AI retrieval/training agents, including GPTBot, ClaudeBot, Claude-User, Google-Extended, and MistralAI-User.
- The sampled article [`The first self-driving vehicle on Mars has proven to be a smashing success`](https://arstechnica.com/space/2026/08/the-first-self-driving-vehicle-on-mars-has-proven-to-be-a-smashing-success/) returned 200 to the project User-Agent. No response `Content-Signal` or `X-Robots-Tag` widened permitted use.
- Ars' [reprint guidance](https://arstechnica.com/reprints/) says readers may make copies for personal, private offline use, but may not republish online or in print or distribute to others without permission. The [subscription FAQ](https://arstechnica.com/ars-subscription-faq/) says personalized full-text feeds must not be shared.

Inference: the ordinary acquisition route and one-user offline Edition have publisher support. Remote LLM disclosure does not.

### SVT Nyheter

- [`https://www.svt.se/robots.txt`](https://www.svt.se/robots.txt) returned 200. Its wildcard group allows `/` and declares `ai-train=no, search=yes, ai-input=yes`; comments say journalism is available for public search and real-time retrieval, not AI training. It separately allows named retrieval agents and blocks named training agents.
- [`https://www.svt.se/rss.xml`](https://www.svt.se/rss.xml) returned 200 as XML with `© Sveriges Television AB`, descriptions only, and no licence link. The sampled article [`I fel händer kan AI vara ett mycket farligt vapen`](https://www.svt.se/nyheter/inrikes/i-fel-hander-kan-ai-vara-ett-mycket-farligt-vapen) returned 200 to the project User-Agent.
- SVT's [material-use guidance](https://www.svt.se/kontakt/kopa-visa-och-forska-pa-svts-program-och-material) emphasizes that its material is copyright-protected, that third-party rights may exist, and that users must make their own assessment. It does not provide a general full-text feed or redistribution licence.

Inference: SVT provides the clearest affirmative AI-input signal of the four Sources. It can support `remote_allowed` only for non-training inference with provider retention controls and operator-approved private copying. The signal does not authorize public distribution, fine-tuning, or third-party media reuse.

### Sveriges Radio Ekot

- [`https://api.sr.se/robots.txt`](https://api.sr.se/robots.txt) returned 200 and allows `/api/rss/` for `*`, while disallowing the rest of the host and named AI crawlers. The Ekot feed [`https://api.sr.se/api/rss/program/83`](https://api.sr.se/api/rss/program/83) returned 200 as Atom and declares `Copyright Sveriges Radio 2026. All rights reserved.` No `rel=license` was observed.
- Sveriges Radio's [Open API page](https://www.sverigesradio.se/oppetapi) says the API remains usable only when its terms are followed. The current [API terms](https://www.sverigesradio.se/artikel/api-villkor), updated 2025-08-29, say API Material may be used only through linking and/or streaming for reception in Sweden; it must not be stored, downloaded, copied, preserved, or modified except for temporary copies. They also prohibit machine learning without SR's prior approval, require clear attribution, prohibit advertising directly against the Material, and reserve the right to change the terms.
- The sampled article page [`https://www.sverigesradio.se/artikel/9272244`](https://www.sverigesradio.se/artikel/9272244) and [`https://www.sverigesradio.se/robots.txt`](https://www.sverigesradio.se/robots.txt) returned 403 to the project User-Agent. This confirms a technical access control independently of the API-host allowance.

Result: research #2's provisional `DEGRADED_SOURCE_BODY` route must not be implemented under current evidence. The feed may support a transient, attributed link-only record, but not a retained body, generated summary, or full-text Edition without SR approval.

## Standards and signal interpretation

- [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309.html) controls crawler URI access. Match the case-insensitive product token, merge exact matching groups, otherwise use `*`, and choose the most-specific path. `Allow`, `Disallow`, and `User-agent` are standard; fields such as `Content-Signal` are extensions.
- There is no final universal AI-use RFC. The IETF [AI Preferences working group](https://datatracker.ietf.org/wg/aipref/about/) is active. Its [vocabulary draft](https://datatracker.ietf.org/doc/html/draft-ietf-aipref-vocab) is work in progress, treats absence as unknown, and warns that signals do not themselves create rights or obligations.
- Cloudflare documents its managed [`Content-Signal`](https://developers.cloudflare.com/bots/additional-configurations/managed-robots-txt/) categories `search`, `ai-input`, and `ai-train`; a missing category neither grants nor restricts permission through that signal. Store the issuer/version and raw value rather than pretending it is RFC syntax.
- Provider bot tokens are purpose-specific. [OpenAI's crawler documentation](https://developers.openai.com/api/docs/bots) distinguishes search and training crawlers; it does not say that allowing or blocking them grants a third party permission to upload already-acquired text to an API.
- The [W3C Text and Data Mining Reservation Protocol](https://www.w3.org/community/reports/tdmrep/CG-FINAL-tdmrep-20240510/) is a Community Group report, not a W3C Standard. Recognize its `tdm-reservation` metadata as a review/deny signal, not a complete licence.
- RSS is a syndication format, not a licence. The [RSS 2.0 specification](https://www.rssboard.org/rss-specification) defines optional copyright/source metadata. [Atom RFC 4287](https://www.rfc-editor.org/rfc/rfc4287.html) defines human-readable rights inheritance; [RFC 4946](https://www.rfc-editor.org/rfc/rfc4946.html) defines `rel=license`. Missing rights/licence metadata means unknown, not public domain.

For the first remote adapter, official [OpenAI API data controls](https://developers.openai.com/api/docs/guides/your-data) say API data is not used for training unless the customer opts in, but default abuse-monitoring logs may contain prompts/responses for up to 30 days. Responses application state is retained for at least 30 days by default; use `store: false`. Zero Data Retention or Modified Abuse Monitoring requires approval. Consequently, an OpenAI provider preflight must record the actual project controls and must not claim zero retention merely because `store: false` is set.

## Operator decision algorithm

Evaluate every Source and every distinct origin before fetching content:

1. **Identify the route.** Record feed URL, page URL pattern, final redirect origin, exact User-Agent product token, acquisition mode, intended LLM use, intended retention, audience, and jurisdiction/rights-basis attestation.
2. **Fetch policy evidence.** Retrieve robots for each origin with the production User-Agent. Record HTTP status, retrieval time, raw hash, matched group/rule, `Content-Signal`, `X-Robots-Tag`, TDM metadata, feed rights/copyright/licence fields, applicable terms URL/version, page-level licence metadata, and technical status.
3. **Gate acquisition.** Disable a route on a matching `Disallow`, applicable terms prohibition, 401/403/451, or unresolved network/5xx robots failure. A robots 404/no matching rule passes only the REP gate. Never change identity, proxy, headers, route, or mirror to bypass a block.
4. **Gate retention/distribution.** Require an explicit licence/term or an operator-attested lawful private-use basis. Enforce audience `single_operator`, immutable source attribution, canonical link, no public artifact/cache/log, and separate rights for images/audio. If uncertain, do not persist the body.
5. **Gate local LLM use.** Acquisition and retention must already pass. Any applicable machine-learning/AI-input prohibition makes the Source `disabled` pending permission. Otherwise missing/ambiguous remote permission resolves to `local_only`; never train or fine-tune.
6. **Gate remote LLM use.** Require an affirmative source-specific permission or operator-owned rights grant, Publication opt-in, and an approved provider profile. The profile must state training use, application-state retention, abuse retention, region, subprocessors/tools, and request mode. Any unknown, provider mismatch, opt-in-to-training, or unexpected retention resolves to `local_only`.
7. **Intersect policies at Run time.** A Publication cannot widen a Source. A model fallback cannot inherit another provider's approval. Mixed-Source prompts include only Articles whose Source and provider profile both permit that exact processing.
8. **Record the decision.** Store evidence hashes, URLs, timestamps, matched rules, decision code, reviewer, review expiry, and the effective policy. Do not store fetched Article bodies in this public evidence record.

Suggested configuration additions:

```yaml
sources:
  svt-nyheter:
    feed_url: https://www.svt.se/rss.xml
    acquisition: web
    llm_processing: remote_allowed
    rights:
      basis: operator_attested_private_use
      audience: single_operator
      attribution_required: true
      media_reuse: false
    eligibility:
      evidence_reviewed_at: 2026-08-09
      review_expires_at: 2026-09-08
      evidence_id: svt-20260809

remote_providers:
  openai-editorial:
    training_opt_in: false
    store: false
    max_abuse_retention_days: 30
    tools: none
```

Schema/runtime rules:

- `llm_processing` is required and enum `disabled | local_only | remote_allowed`; omission defaults to `local_only`, never `remote_allowed`.
- `remote_allowed` requires a rights basis, unexpired evidence, Publication opt-in, and a named provider profile. It is invalid with `training_opt_in: true` or unknown retention/subprocessor fields.
- `metadata_only` must guarantee temporary parsing and persist only title, canonical link, published time, Source attribution, and evidence/identity hashes—never feed summary/content, article body, image, or audio.
- State Store and private EPUB retention are distinct fields. Diagnostic/LLM Evidence Record retention never authorizes retaining Source content.
- The deterministic body route from research #2 remains subordinate to this eligibility gate.

## Change monitoring and failure policy

- Cache a successful robots decision no longer than 24 hours. Re-fetch before use after expiry and after any redirect-origin change.
- Treat robots network/5xx failures as complete disallow until a successful fetch. Treat 401/403/451 as route-disabled. Treat 404 as REP-neutral but require the non-robots rights gates.
- Hash and compare robots, applicable terms sections, feed rights/licence fields, response machine-use headers, and relevant page metadata. Review at least every 30 days and immediately on any diff, provider-policy change, new origin, new model/provider, or new use.
- A new restriction takes effect before the next acquisition or LLM call. Downgrade `remote_allowed` to `local_only` on remote ambiguity; disable acquisition/processing when the changed scope cannot be determined safely. Existing private Editions remain immutable, but prohibited retained working copies expire/delete according to their recorded policy.
- Terms pages that cannot be fetched by the production identity require manual review from a normal, non-evasive browser or publisher contact. Do not infer permission from a search snippet or cached copy.
- Emit sanitized decision codes such as `SOURCE_ROBOTS_DENIED`, `SOURCE_TERMS_DENIED`, `SOURCE_RIGHTS_REVIEW_EXPIRED`, `SOURCE_REMOTE_NOT_ALLOWED`, and `SOURCE_ACCESS_CONTROLLED`; never echo Article text or raw terms into public Actions logs.

## Risks and unresolved facts

- Copyright exceptions, private-copy rules, contractual enforceability, and text/data-mining reservations vary by operator, jurisdiction, and facts. This report intentionally requires operator attestation rather than making a legal conclusion.
- `Content-Signal`, TDM reservations, provider crawler tokens, and the IETF AI-preferences drafts differ in scope and maturity. A parser can preserve them without equating them.
- An affirmative `ai-input=yes` does not identify every permitted provider, retention period, region, or subprocessor. SVT's conditional remote classification should be re-reviewed before first use.
- Article bodies may contain wire copy, photographs, video, music, or other third-party material with narrower rights than the page text. Default media reuse to false.
- Publisher terms and edge behavior can differ by hostname and geography. Evaluate every final origin from the actual production environment.
- The current project's Source registry has no rights-basis, evidence-expiry, metadata-only, or provider-retention fields; implementation must add and validate them before enabling scheduled acquisition.

## Implementation-ready acceptance checks

1. A fixture with `User-agent: epub-news-feeder` + `Disallow: /news/` blocks `/news/x`; a wildcard allow does not override the exact matching group.
2. Robots network/5xx/unparseable failure fails closed; 404 passes only the REP test and still fails remote processing without affirmative evidence.
3. A redirected feed/page triggers independent robots and terms evaluation for the final origin before its body is read.
4. `ai-input=no`, an applicable TDM reservation, SR-style machine-learning prohibition, 401/403/451, or disallowed path produces the expected stable deny code without retry/evasion.
5. Missing `llm_processing` becomes `local_only`; `remote_allowed` fails schema/preflight without rights basis, unexpired evidence, Publication opt-in, and a fully declared provider profile.
6. The remote prompt builder excludes every Article not `remote_allowed` for the selected provider; mixed-source requests cannot leak `local_only` text.
7. OpenAI Responses requests set `store: false`, use no tools, do not opt into data sharing/training, and record the configured abuse-retention profile; tests do not describe this as zero retention without approved ZDR.
8. `metadata_only` persists no feed summary/content, Article body, image, or audio bytes. An Ekot fixture produces only title/link/time/Source attribution and never enters an LLM prompt.
9. EPUB output includes one Canonical Rendition with Source, byline when supplied, canonical publisher link, copyright where supplied, and no uncleared third-party media. No Edition/artifact/log is public.
10. Evidence older than its review expiry or changed evidence hashes downgrade/disable before fetch or LLM calls; existing immutable Editions are not rewritten.
11. Source eligibility runs before the full-text acquisition rules from issue #2, so an otherwise structurally complete body cannot bypass rights/policy denial.
12. Tests cover all four initial Sources with the matrix above and assert Ekot cannot produce `DEGRADED_SOURCE_BODY` under current evidence.

## Recommended next action

Implement the eligibility registry/gate before any real scheduled Source fetch or LLM adapter. Seek written clarification from Sveriges Radio before using Ekot beyond transient attributed links. Reconfirm SVT's intended scope and the chosen remote provider's retention controls before the first `remote_allowed` Run.
