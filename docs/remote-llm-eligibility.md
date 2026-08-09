# Remote LLM Eligibility Evidence

**Question (GitHub issue #44):** For each configured Source, what do current primary sources permit
for sending acquired Article text to a remote third-party LLM provider — for example OpenAI — for
private, single-operator summarisation?

**Reviewed:** 2026-08-09.
**Suggested review expiry:** 2026-09-08 (30 days), and immediately on any change to a publisher's
terms, robots signal, or the provider's data-handling documentation. This matches the
`review_expires_at` already recorded in `examples/reality-check.yaml`.

**Fetch identity:** `epub-news-feeder research contact: https://github.com/davelowelarsson/epub-news-feeder`.
Where that identity was refused, the page was read manually in an ordinary browser without changing
identity, proxying, or bypassing any control; that is recorded per Source.

**Scope.** This is dated operational policy evidence for the `LLM Processing Policy` gate. It is not
legal advice and is not a finding that any particular use is or is not lawful in any jurisdiction.

**What this document may and may not do.** It records what the evidence says. It has no authority to
widen a route. Where no clause addresses remote LLM processing, the verdict is `unknown`, and
`unknown` is never `allow`. Public readability of a page is not permission. Absence of a prohibition
is not permission.

## Three independent permissions

Following issue #15, this review keeps three questions apart, because the project treats them as
independent gates and a publisher may answer them differently:

- **(a) Reproduction / redistribution** — may the text be copied, retained, or delivered in an
  Edition at all?
- **(b) Automated or machine processing / text and data mining** — may the text be processed by
  software, crawled, mined, or extracted?
- **(c) AI / LLM use specifically** — and within that, training/fine-tuning versus inference-time use
  such as grounding, retrieval-augmented generation, or summarisation.

Issue #44 asks only about the remote limb of (c): disclosure of already-acquired Article text to a
third-party provider for inference. Passing (a) or (b) never passes (c), and permission for local
inference never implies permission for remote inference, because remote inference adds disclosure to
a third party.

---

## Source 1 — David Lowe Larsson (`davidlowelarsson.com`)

Currently recorded: `remote_llm: unknown`.

### URLs consulted

| URL | Status | Note |
| --- | --- | --- |
| `https://davidlowelarsson.com/robots.txt` | 200 | Cloudflare managed content signals block plus a site group |
| `https://davidlowelarsson.com/rss.xml` | 200 | `application/xml`, full `content:encoded` bodies |
| `https://davidlowelarsson.com/sitemap-index.xml` → `sitemap-0.xml` | 200 | 17 URLs total |

### Quoted clauses

The robots file carries the Cloudflare managed preamble, which is the only text on the site that
speaks to AI use at all:

> As a condition of accessing this website, you agree to abide by the following content signals:
> (a) If a Content-Signal = yes, you may collect content for the corresponding use.
> (b) If a Content-Signal = no, you may not collect content for the corresponding use.
> (c) If the website operator does not include a Content-Signal for a corresponding use, the website
> operator neither grants nor restricts permission via Content-Signal with respect to the
> corresponding use.

It defines the vocabulary, including:

> ai-input: inputting content into one or more AI models (e.g., retrieval augmented generation,
> grounding, or other real-time taking of content for generative AI search answers).
> ai-train: training or fine-tuning AI models.

The matching wildcard group declares:

> User-agent: *
> Content-Signal: search=yes,ai-train=no,use=reference
> Allow: /

Named agents are separately excluded, each with `Disallow: /`: `Amazonbot`, `Applebot-Extended`,
`Bytespider`, `CCBot`, `ClaudeBot`, `CloudflareBrowserRenderingCrawler`, `Google-Extended`,
`GPTBot`, `meta-externalagent`.

### Reading

The declared signal sets `search` and `ai-train`. It does **not** set `ai-input`. By clause (c) of
the file's own preamble, the operator therefore *neither grants nor restricts* permission for
inputting content into an AI model. `use=reference` describes how an AI system may consume content
if it may consume it; it is not itself an `ai-input` grant, and reading it as one would be inferring
permission from silence.

The `GPTBot` and `ClaudeBot` exclusions are crawler product tokens under RFC 9309. They govern
whether those crawlers may fetch the site. They say nothing either way about a third party uploading
already-acquired text to a provider API, and per issue #15 they are not treated as an API permission
signal in either direction.

The site publishes no terms of use, licence, copyright, or colophon page: the sitemap enumerates the
home page, four category indexes, a post index, and eleven posts, and nothing else. The RSS channel
carries no `copyright` element and no `rel=license`.

**Separately: the operator's own attestation.** `examples/reality-check.yaml` records
`basis: operator_attested_private_use` and `copyright_notice: Copyright David Lowe Larsson`. If the
operator of this project is in fact the rights holder for `davidlowelarsson.com`, then the operator
can simply grant this permission by attesting to it, and no publisher-facing evidence is required.
That is a route this document can point at but cannot exercise. Two things must be said plainly:

1. Nothing fetched in this review establishes that the operator of this repository owns or controls
   `davidlowelarsson.com`. The rights basis in the configuration is a configuration assertion, not
   primary evidence of ownership. Ownership is not inferred from a matching name.
2. If an attestation is recorded, it should be recorded as its own dated evidence with its own
   `evidence_id` — an operator grant, distinct from the publisher-signal evidence above — so that a
   later reviewer can see that the permission came from the operator and not from the site.

### Verdict

**`unknown`.** No published clause addresses inputting this site's content into an AI model, remote
or otherwise. The one machine-readable signal deliberately leaves `ai-input` unset, and the file
itself says an unset signal grants nothing. Under the fail-closed rule this stays `local_only`.

It may become `allow` only if the operator records an explicit, dated attestation that they are the
rights holder and grant remote processing. It does not become `allow` on the strength of this review.

---

## Source 2 — Ars Technica (`arstechnica.com`, published by Condé Nast)

Currently recorded: `remote_llm: deny`.

### URLs consulted

| URL | Status | Note |
| --- | --- | --- |
| `https://www.condenast.com/user-agreement/` | 200 | "Last Updated: October 10, 2024" |
| `https://arstechnica.com/reprints/` | 200 | Reprint and personal-use guidance |
| `https://arstechnica.com/ai-policy/` | 200 | "This policy was last updated April 22, 2026." |
| `https://arstechnica.com/amendment-to-conde-nast-user-agreement-privacy-policy/` | 200 | Ars-specific addendum |
| `https://arstechnica.com/robots.txt` | 200 | Named AI agents excluded |
| `https://feeds.arstechnica.com/arstechnica/index` | 200 | No `copyright`, no `rel=license` |

Every Ars page footer states the applicable agreement:

> © 2026 Condé Nast. All rights reserved. Use of and/or registration on any portion of this site
> constitutes acceptance of our User Agreement and Privacy Policy and Cookie Statement and Ars
> Technica Addendum and Your California Privacy Rights. […] The material on this site may not be
> reproduced, distributed, transmitted, cached or otherwise used, except with the prior written
> permission of Condé Nast.

### Quoted clauses — (c) AI / LLM use

Condé Nast User Agreement, "Rules of Usage — Use of the Service by You":

> Unless otherwise specified, the Service is intended for your personal, non-commercial use only.
> You may not access visit, use and/or store the Service or any of its Content except for personal,
> noncommercial use. Non-commercial use does not include use of the Service— except with prior
> written consent—in connection with the development, training, fine tuning, grounding (including
> through retrieval-augmented generation (RAG)), of any large language model, foundation model, deep
> machine learning, generative artificial intelligence model or algorithm, or any software or tool
> that incorporates generative artificial intelligence.

### Quoted clauses — (b) automated processing and text/data mining

Condé Nast User Agreement, "Prohibitions on Use of the Service":

> use any bots, cheats, macros, scripts, or run Maillist, Listserv or any form of autoresponder, or
> use any other automated process, or engage in meta-searching or periodic caching of information,
> to access, visit and/or use the Service […]; copy, harvest, crawl, index, scrape, spider, mine,
> gather, extract, compile, obtain, aggregate, capture, access, store, or republish any Content on
> or through the Service, including by an automated or manual process or otherwise, for any and all
> purposes other than indexing Content for inclusion in a Search Engine, including but not limited
> to any purpose related to data mining and/or the training, development, testing, fine-tuning,
> improvement, grounding (including through RAG) or operation of any software or service, including
> any architectures, models, or weights contained therein, to the extent that it incorporates a
> large language model, foundation model, deep machine learning, generative artificial intelligence,
> or any other process of a nature commonly referred to as artificial intelligence

The agreement binds automated agents explicitly, in its definitions:

> "You" or "Your", whether capitalized or not, means all those who access, visit and/or use the
> Service, whether acting as an individual or on behalf of an entity, including you and all persons,
> entities, or digital engines of any kind that harvest, crawl, index, scrape, spider, or mine
> digital content by an automated or manual process or otherwise.

### Quoted clauses — (a) reproduction and redistribution

Ars Technica reprints page, "Personal & private offline use":

> Copyright law in the United States permits readers to make copies of our work, but only for private
> use. You may not republish these materials online or in print, or distribute them to others,
> without explicit permission.

### Pages that do *not* bear on this question

- **Ars Technica's generative-AI policy** (last updated 22 April 2026) is inbound-facing only. It
  opens: "This is our policy on the use of generative AI in Ars Technica's editorial work. It
  applies to all editorial work produced by Ars Technica's writers, editors, and contributors."
  It governs Ars' newsroom, not third parties processing Ars content, and grants nothing here.
- **The Ars Technica addendum** replaces Section VI(2)(B) of the Condé Nast agreement and concerns
  ownership and licence of *content a user posts to* Ars. It does not touch use of Ars' journalism.
- **robots.txt** excludes a long list of AI retrieval and training agents (`GPTBot`,
  `Google-Extended`, `ClaudeBot`, `Claude-User`, `Claude-SearchBot`, `PerplexityBot`,
  `Perplexity-User`, `MistralAI-User`, `CCBot`, `cohere-ai`, `Bytespider`, `DuckAssistBot`,
  `anthropic-ai`, and others) with `Disallow: /`. This is corroborating direction of travel, not the
  operative clause; the operative clause is the User Agreement.

### Verdict

**`deny`.** This is the clearest and most explicit of the four. Condé Nast prohibits, absent prior
written consent, use of the Content "in connection with the … grounding (including through
retrieval-augmented generation (RAG)) of any large language model … or any software or tool that
incorporates generative artificial intelligence". Sending an acquired Ars Article body to a remote
provider so that a model can summarise it is squarely within that description. The prohibition is
express, so nothing turns on silence, and no provider-side control can cure it — a data-handling
guarantee does not create a licence.

**Flag, outside the scope of this ticket.** Both quoted clauses are drafted by *purpose*, not by
*deployment location*. They prohibit grounding and RAG "of any large language model" and any use
"related to data mining and/or the … grounding (including through RAG) or operation of any software
or service … to the extent that it incorporates a large language model", with no carve-out for
locally hosted models. On its face that reaches the project's local Ollama route as well. The
current configuration records `local_llm: allow` for Ars on the strength of the private-offline-copy
reprint guidance, which is a permission about (a) reproduction and is silent on (c). Issue #44 asks
only about the remote gate, so this document records the tension and changes nothing. It should be
raised as its own decision.

---

## Source 3 — SVT Nyheter (`svt.se`)

Currently recorded: `remote_llm: conditional`.

### URLs consulted

| URL | Status | Note |
| --- | --- | --- |
| `https://www.svt.se/robots.txt` | 200 | The only clause anywhere that addresses AI input |
| `https://www.svt.se/rss.xml` | 200 | `<copyright>© Sveriges Television AB</copyright>` |
| `https://www.svt.se/kontakt/kopa-visa-och-forska-pa-svts-program-och-material` | 200 | Programme/material rights guidance |
| `https://kontakt.svt.se/guide/rattigheter` | 301 → `https://www.svt.se/kontakt/` | Superseded |

### Quoted clauses

`https://www.svt.se/robots.txt`, file header comment:

> SVT robots.txt – governs AI and search access to SVT content
>
> SVT's journalism and content is available for public search indexing and real-time retrieval. Use
> of our content to train AI models is not permitted. This file reflects SVT's commitment to
> transparency about how our content may and may not be used.

The matching wildcard group:

> # DEFAULT: Global preferences
> # SVT allows search indexing and real-time retrieval, but not AI training.
> User-agent: *
> Content-Signal: ai-train=no, search=yes, ai-input=yes
> Allow: /

An explicitly labelled section then admits named AI retrieval agents with the same signal:

> # ALLOWED: AI retrieval (not training)
> # These crawlers fetch SVT content in real time to answer user questions.
> # They do not use the content to train AI models.

covering `ChatGPT-User` ("OpenAI's retrieval bot — used when a user asks ChatGPT to browse the
web"), `OAI-SearchBot`, `Claude-User`, `Claude-SearchBot`, `PerplexityBot`, and `MistralAI-User`. A
"DISALLOWED: AI training crawlers" section excludes `GPTBot`, `ClaudeBot`, `anthropic-ai`,
`Google-Extended`, `Bravebot`, `CCBot`-class harvesters, `img2dataset`, `FriendlyCrawler`,
`Webzio-Extended`, `ImagesiftBot`, `iaskspider/2.0`, `YouBot`, and others, each `Disallow: /`.

The material-use page speaks only to programmes and third-party rights, not to machine processing:

> "SVT:s undertexter, översättningar och manus är upphovsrättsskyddade texter"
> — *SVT's subtitles, translations and scripts are copyright-protected texts.*

> "Det inte bara är SVT som har rättigheter till våra program"
> — *It is not only SVT that holds rights to our programmes.*

> "SVT har inte möjlighet att ge enskild juridisk rådgivning"
> — *SVT is not able to give individual legal advice.*

That page contains no mention of AI, maskininlärning, or automated processing. SVT's published AI
policy material (via `omoss.svt.se`) governs SVT's *own* editorial use of AI and its transparency
obligations to its audience; it says nothing about third parties processing SVT content.

### Reading

SVT gives the only affirmative AI-input signal among the four Sources, and gives it deliberately:
`ai-input=yes` is set alongside `ai-train=no`, and the accompanying prose spells out the distinction
between real-time retrieval and training. Under the Cloudflare content-signal vocabulary, `ai-input`
is exactly "inputting content into one or more AI models (e.g., retrieval augmented generation,
grounding, or other real-time taking of content …)", which is what this project's summarisation does.

But it stops well short of `allow`, for reasons that are properties of the evidence and not of
caution:

1. **It is a signal, not a licence, and not an RFC.** `Content-Signal` is a Cloudflare extension to
   robots.txt. It expresses a permission to collect for a use; it does not name a permitted
   provider, a retention period, a processing region, or a subprocessor limit, and it does not
   address retention or distribution of the text at all.
2. **Its own framing is real-time retrieval by a user-triggered agent.** The named allowances are
   fetch-time bots (`ChatGPT-User`, `Claude-User`) that retrieve a page on a user's behalf. The
   closest analogue to this project — a private single-operator digest that summarises text it
   already acquired — is adjacent to that, not identical to it.
3. **No SVT terms page corroborates it.** The one rights-facing page is about programmes and
   third-party clearances and does not mention machine processing. So the signal stands alone.
4. **It says nothing about (a).** `© Sveriges Television AB` on the feed still governs retention and
   distribution, which remain separate gates handled by the operator's private-use basis.

### Verdict

**`conditional`, unchanged.** Remote LLM processing of SVT Article text may be enabled only while
*all* of the following hold, and it degrades to `local_only` the moment any one is unproven:

1. Publication opt-in is explicitly set (`editorial.remote_processing: true`) and a named
   `RemoteProviderProfile` is referenced — a Publication may never widen a Source.
2. The evidence above is unexpired and the live `Content-Signal` still reads `ai-input=yes` with
   `ai-train=no`. A change to either value takes effect before the next LLM call.
3. The provider profile proves **no training**: `training_opt_in: false`, contractually and not only
   as a documented default, and no fine-tuning, distillation, or evaluation reuse of the Input.
4. The provider profile proves **no durable provider-side application state**: `store: false`, with
   `application_state_retention_days` declared honestly rather than assumed to be zero.
5. Abuse-monitoring retention is **bounded and disclosed**, with `max_abuse_retention_days` set to
   the provider's actual published figure.
6. `tools: none` — no web search, no file search, no code interpreter, no remote MCP server, since
   each is an additional disclosure path outside the approved profile.
7. `subprocessors` is fully declared, and no subprocessor beyond the approved provider receives the
   Article text. **See the provider section below: this limb is the one that currently fails.**
8. Attribution and single-operator audience are preserved, and no publisher media is reused.

---

## Source 4 — Sveriges Radio Ekot (`sverigesradio.se`, feed at `api.sr.se`)

Currently recorded: `remote_llm: deny`.

### URLs consulted

| URL | Status | Note |
| --- | --- | --- |
| `https://www.sverigesradio.se/artikel/api-villkor` | 403 to project identity; read manually in an ordinary browser | "Uppdaterad 29 aug 2025 · kl 15:57" |
| `https://api.sr.se/robots.txt` | 200 | `Allow: /api/rss/`, `Disallow: /` otherwise |
| `https://api.sr.se/api/rss/program/83` | 200 | Atom; `<rights>Copyright Sveriges Radio 2026. All rights reserved.</rights>` |
| `https://www.sverigesradio.se/robots.txt` | 403 | Edge access control; not retried or bypassed |
| `https://www.sverigesradio.se/oppetapi` | 403 to project identity | Same host control |

The terms host refuses this project's identity at the edge (Akamai "Access Denied"). The terms were
therefore read manually in an ordinary browser, without spoofing a crawler token, proxying, or
bypassing any control — the fallback that issue #15 prescribes for exactly this case. The 403 is
itself recorded evidence: it is a technical access control on `www.sverigesradio.se`, independent of
what `api.sr.se/robots.txt` allows.

### Quoted clauses

Page status:

> "OBS! Sveriges Radios Öppna API underhålls inte längre, men går fortfarande att använda."
> — *Note: Sveriges Radio's Open API is no longer maintained, but can still be used.*

Scope of the permission (this is (a) and (b) together):

> "Innehåll som finns i API:t kallas Materialet. Det får endast användas genom länkning och/eller
> streaming för mottagning i Sverige. Materialet får således inte lagras, laddas ner, kopieras eller
> bevaras på annat sätt än genom tillfälliga kopior (i enlighet med 11 a § upphovsrättslagen) av den
> tjänst som använder API:t. Materialet får följaktligen inte modifieras på något sätt."
>
> — *Content available in the API is called the Material. It may only be used through linking and/or
> streaming for reception in Sweden. The Material may therefore not be stored, downloaded, copied or
> preserved other than through temporary copies (in accordance with § 11 a of the Copyright Act) by
> the service using the API. The Material may consequently not be modified in any way.*

The AI clause, (c), stated in a single sentence:

> "Materialet får inte användas för maskininlärning utan Sveriges Radios föregående godkännande."
>
> — *The Material may not be used for machine learning without Sveriges Radio's prior approval.*

Attribution:

> "Det ska tydligt framgå att Materialet kommer från Sveriges Radio."
> — *It must be clearly evident that the Material comes from Sveriges Radio.*

Mutability of the terms:

> "Dessa villkor kan ändras av Sveriges Radio vid behov."
> — *These terms may be changed by Sveriges Radio as needed.*

The page also gives named contacts, including `tomas.granryd@sr.se` for "Om du vill använda
Materialet på annat sätt" (*if you wish to use the Material in another way*).

### Reading

Three independent bars, any one of which is sufficient:

- **(c) directly.** "Maskininlärning" is the Swedish term for machine learning. Submitting Material
  as input to a large language model for summarisation is use of the Material by a machine-learning
  system. Even on the narrowest possible reading — that "maskininlärning" means only training — the
  clause is a *prohibition subject to prior approval*, so the permissive reading would have to come
  from silence, and silence is `unknown`, never `allow`.
- **(a) and (b).** Remote processing requires transmitting a copy of the body to a provider. The
  terms permit no copy beyond a temporary copy under § 11 a URL made by the service using the API,
  and prohibit modification. A summary is a derived work product produced from a disclosed copy.
- **Access control.** The publisher pages that would carry the Article body return 403 to this
  project's identity, so there is no eligible body to process in the first place. This is why the
  Source is `acquisition: metadata_only` and `page_acquisition: deny`.

### Verdict

**`deny`.** The clause is express and conditional on approval that has not been sought or granted.
The only route that changes this is Sveriges Radio's prior approval — "utan Sveriges Radios
föregående godkännande" — obtained in writing via the contacts on the terms page. Note also that the
API is described as no longer maintained, which makes the terms more likely to change or lapse
without notice, not less; the 30-day re-review applies with full force.

---

## Remote provider profile — OpenAI, mapped to `RemoteProviderProfile`

### Sources consulted

| URL | Status | Date on page |
| --- | --- | --- |
| `https://developers.openai.com/api/docs/guides/your-data` (`.md` variant) | 200 | live |
| `https://openai.com/policies/services-agreement/` | 200 | "Updated: 1 December 2025"; "Effective: January 1, 2026" |
| `https://openai.com/policies/sub-processor-list/` | 200 | "Last updated: 9 July 2026" |

### What the terms actually commit to

**Training.** The documentation states:

> Your data is your data. As of March 1, 2023, data sent to the OpenAI API is not used to train or
> improve OpenAI models (unless you explicitly opt in to share data with us).

and the Services Agreement makes it contractual rather than merely documented, at §4.2:

> OpenAI will only use Customer Content as necessary to provide Customer with the Services, comply
> with applicable law, enforce the OpenAI Policies, and prevent abuse. OpenAI will not use Customer
> Content to develop or improve the Services, unless Customer explicitly agrees to such use.

**Abuse-monitoring retention.**

> Abuse monitoring logs may contain certain customer content, such as prompts and responses, as well
> as metadata derived from that customer content, such as classifier outputs. By default, abuse
> monitoring logs are generated for all API feature usage and retained for up to 30 days, unless
> longer retention is required by law, or is reasonably necessary to protect our services or any
> third party from harm.

**Application state.** The per-endpoint table gives `/v1/responses` and `/v1/chat/completions` a
30-day abuse-monitoring retention and "None, see below for exceptions" application-state retention.
The exceptions matter:

> The Responses API has a 30 day Application State retention period by default, or when the `store`
> parameter is set to `true`. Response data will be stored for at least 30 days.

> Prompt caching may store encrypted key/value tensors in GPU-local storage as application state.
> This data is stored on the local GPU machines and is not retained after the 24-hour expiration.

> When Zero Data Retention is not enabled for an organization, all queries use extended prompt
> caching for all supported models.

Background mode "stores response data to disk for roughly 10 minutes to enable polling."

**Zero Data Retention and Modified Abuse Monitoring.** Neither is self-serve:

> Eligible customers may have their customer content excluded from these abuse monitoring logs,
> subject to the limitations below, by getting approved for the Zero Data Retention or Modified
> Abuse Monitoring controls. Currently, these controls are subject to prior approval by OpenAI and
> acceptance of additional requirements.

With ZDR, "the `store` parameter for `/v1/responses` and `v1/chat/completions` will always be
treated as `false`, even if the request attempts to set the value to `true`." OpenAI also reserves
"Eyes Off" and "Safety Retention" carve-outs under which content may be retained and human-reviewed
notwithstanding ZDR/MAM, on advance written notice.

**Region.** Data residency is a project configuration but is also not self-serve:

> Contact our sales team to see if you're eligible for using data residency controls.

An EU endpoint exists (`eu.api.openai.com`, Europe — EEA + Switzerland) supporting both regional
storage and regional processing for `/v1/responses` and `/v1/chat/completions`, but the table marks
it "MAM or ZDR required", and:

> To use data residency with any region other than the United States, you must be approved for abuse
> monitoring controls, and execute a Modified Retention amendment.

**Subprocessors.** The list of 9 July 2026 names, for the API specifically, at minimum: Cloudflare,
Microsoft, CoreWeave, Oracle Cloud Infrastructure, Google Cloud Platform, Amazon Web Services,
Cerebras, Snowflake\*, TaskUs, Intercom, Salesforce, Pylon Labs, Accenture, Fivetran, Confluent\*,
Cinder Technologies\*, and Okta — where `*` means "Except where Zero Data Retention (ZDR) is used".
Three of these are content-moderation paths, and the list is explicit that customer content can
reach them:

> For content that OpenAI's models flag as being in violation of OpenAI's policies, OpenAI may share
> samples of the flagged Customer Content with relevant Sub-processors to assist OpenAI in its
> review and enforcement.

TaskUs processes in the Philippines; Accenture in the United States, Canada, and the Philippines.

### Mapped to `RemoteProviderProfile`

Two honest profiles. The left column is what an ordinary operator can configure today without a
sales conversation; the right is what becomes available after approval.

| Field | Default OpenAI API (no ZDR/MAM) | With approved ZDR + EU residency |
| --- | --- | --- |
| `training_opt_in` | `false` — documented and contractual (§4.2) | `false` |
| `store` | `false` — must be set explicitly; the Responses default is `true`-equivalent | `false` (forced by ZDR) |
| `application_state_retention_days` | `1` — not `0`. With `store: false` the endpoint table says "None", but extended prompt caching is on for all queries when ZDR is not enabled and holds encrypted KV tensors up to 24 hours | `0`, if background mode, hosted containers, and file endpoints are unused |
| `max_abuse_retention_days` | `30` | `0`, subject to the Eyes Off / Safety Retention carve-outs and to CSAM-classifier retention of image/file inputs |
| `region` | `null` — no residency guarantee; US and other subprocessor locations apply | `"eu"` via `eu.api.openai.com`, with both regional storage and regional processing for `/v1/responses` |
| `tools` | `"none"` — achievable and required; web search, file search, code interpreter, and remote MCP each open a disclosure path OpenAI states is governed by the third party's own policy | `"none"` |
| `subprocessors` | **Cannot be `null` or empty.** Must enumerate the API-applicable list above, including the moderation path that can receive flagged content for human review | Shorter — Snowflake, Confluent, and Cinder drop out under ZDR — but never empty: infrastructure subprocessors remain |

### Can this satisfy a merely-conditional publisher clause?

Partly, and less than it first appears.

**It satisfies, on the default profile:** no training (limb 3 of the SVT condition), no durable
application state with `store: false` (limb 4, with the 24-hour prompt-cache caveat recorded rather
than hidden), bounded and disclosed abuse retention of 30 days (limb 5), and `tools: none` (limb 6).

**It does not satisfy, on the default profile:** limb 7. The recorded condition from issue #15 is
"no tools/subprocessors beyond the approved provider". OpenAI's own published list names eighteen
third parties for the API, and states that samples of flagged Customer Content may be shared with
moderation subprocessors — TaskUs in the Philippines, Accenture, and the Cinder platform — for human
review. That is disclosure beyond the approved provider, and it is not something the operator can
switch off from the request. Approved ZDR removes customer content from the abuse-monitoring logs
that feed that path and drops Snowflake, Confluent, and Cinder from the applicable list, but ZDR
requires OpenAI's prior approval and acceptance of additional requirements, and OpenAI expressly
reserves Eyes Off and Safety Retention exceptions on advance notice.

**And it cannot satisfy anything at all on the (a)/(b)/(c) question.** The Services Agreement says
so itself, at §4.3:

> Customer is responsible for all Input and represents and warrants that it has all rights,
> licenses, and permissions required to provide Input to the Services.

and at §3.3(b), among the restrictions, the Customer will not "use the Services or Customer Content
in a way that violates third parties' rights". OpenAI's data-handling commitments are commitments
about what OpenAI will do with text the operator was already entitled to send. They are a *necessary*
condition for a conditional Source and never a *sufficient* one, and they are worthless as a defence
where the publisher's clause is `deny`. The provider warrants nothing about Ars Technica or
Sveriges Radio content; it requires the operator to warrant it.

---

## Summary

| Source | Recorded | Verdict on this evidence | Operative primary clause | What would have to change |
| --- | --- | --- | --- | --- |
| David Lowe Larsson | `unknown` | **`unknown`** | robots.txt sets `search=yes,ai-train=no,use=reference`; `ai-input` is unset, and the file states an unset signal "neither grants nor restricts permission" | A dated operator attestation of rights holder status and an explicit grant, recorded as its own evidence. Not this document's to give |
| Ars Technica | `deny` | **`deny`** | Condé Nast User Agreement (10 Oct 2024): no use "in connection with the … grounding (including through retrieval-augmented generation (RAG)) of any large language model … except with prior written consent" | Prior written consent from Condé Nast |
| SVT Nyheter | `conditional` | **`conditional`** — and the condition is currently **not met** by a default OpenAI profile | robots.txt `Content-Signal: ai-train=no, search=yes, ai-input=yes`, with an explicit "AI retrieval (not training)" allowance | Approved ZDR (and preferably EU residency) to close the subprocessor limb; or an explicit narrowing of the recorded condition, which is a separate operator decision |
| Sveriges Radio Ekot | `deny` | **`deny`** | API terms (29 Aug 2025): "Materialet får inte användas för maskininlärning utan Sveriges Radios föregående godkännande", plus linking/streaming-only and no copies beyond temporary | Written prior approval from Sveriges Radio |

**Net answer to issue #44: remote LLM eligibility does not widen.** It stays one `unknown`, two
`deny`, and one `conditional` whose condition a default remote profile does not currently meet. The
production profile that "sends Article text to a remote provider under GitHub Actions" reaches, at
best, one of four Sources, and only after an OpenAI approval process. No field in
`examples/reality-check.yaml` should change on the strength of this document.

---

## What this evidence does NOT establish

1. **It does not establish that any of these uses is lawful.** Copyright exceptions, private-copying
   rules, text-and-data-mining reservations, and the enforceability of browsewrap terms vary by
   jurisdiction and by facts. This is operational policy evidence, not legal advice.

2. **It does not establish permission from silence.** For David Lowe Larsson there is no clause about
   AI input at all. `unknown` there means *no clause addresses this*, exactly. It does not mean
   "probably fine because it's a small personal site", and it does not mean "probably fine because
   the page is publicly readable".

3. **It does not establish that the operator owns `davidlowelarsson.com`.** Nothing fetched proves
   that. If the operator is the rights holder, they can attest; the attestation is the evidence, and
   it has to be recorded as such.

4. **It does not resolve whether "maskininlärning" covers inference as well as training.** SR's
   clause is read conservatively as covering it. That reading is not a finding about Swedish law or
   about SR's intent; it is the fail-closed default applied to an ambiguous term. Only SR can resolve
   it.

5. **It does not convert SVT's `ai-input=yes` into a licence.** `Content-Signal` is a Cloudflare
   extension, not a standard and not a contract. It grants a use category; it names no provider, no
   retention limit, no region, and no subprocessor boundary. It says nothing about retention or
   distribution of the text, which remain separate gates governed by `© Sveriges Television AB` and
   the operator's private-use basis.

6. **It does not clear reproduction, retention, or distribution for any Source.** A remote-processing
   verdict answers only limb (c). Passing it never passes (a) or (b), and the Edition still has to
   satisfy the acquisition, retention, and private-distribution gates independently.

7. **It does not certify a Model Pair.** Provider-level data handling says nothing about editorial
   quality, verification behaviour, or Editorial Influence. Evidence for a Model Pair is accumulated
   separately through Editorial Evaluation and never transfers between pairings or versions.

8. **It does not establish that any provider control is verified.** Every OpenAI figure here is
   OpenAI's own published statement, read on 2026-08-09. `training_opt_in`, `store`,
   `application_state_retention_days`, and `max_abuse_retention_days` in a `RemoteProviderProfile`
   record what the provider *declares* and what the operator *configures*. Neither is an audit.

9. **It does not cover any other provider.** Nothing here transfers to Anthropic, Google, Mistral,
   Azure OpenAI, or a fallback model behind the same adapter. A provider profile is per-provider, and
   a fallback never inherits another provider's approval.

10. **It does not resolve the Ars local-LLM tension it surfaced.** The Condé Nast grounding/RAG
    clause is drafted by purpose and appears to reach local inference too. This document records that
    and deliberately changes nothing; it needs its own decision.

11. **It does not survive its expiry.** Publisher terms, robots signals, and provider retention
    documentation all change without notice, and both SR and Condé Nast reserve the right to change
    theirs. After 2026-09-08, or after any observed change, these verdicts are stale and the affected
    route degrades to `local_only` or disabled until re-reviewed.
