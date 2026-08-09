# EPUB News Feeder

The publication domain for assembling private, finite news digests from public and operator-configured sources.

## Language

**Environment**:
An isolated operational context, such as development, staging, or production, with independent state and history.
_Avoid_: Instance, workspace

**State Store**:
The private durable record of Article identity, Content Revisions, Source health, Editions, Runs, and Publication history within one Environment.
_Avoid_: Cache, config database

**Publication**:
A configured recurring news product with its own structure, selection rules, budgets, and delivery targets.
_Avoid_: Digest configuration, family feed

**Publication Language**:
The language of Edition navigation, labels, and end matter. It does not translate publisher text or determine an Article Language.
_Avoid_: Article Language, translation language

**Edition**:
One generated EPUB belonging to a Publication and identified by its publication time and Run ID.
_Avoid_: Build, artifact, issue

**Delivery Target**:
An external private destination configured to receive an Edition. Its acknowledgement completes delivery but does not prove that a reader opened or read the Edition.
_Avoid_: State Store, reader

**Pending Delivery**:
The private durable record that correlates an Edition and Run ID with its intended Delivery Target while handoff is incomplete or awaiting reconciliation.
_Avoid_: Upload retry, Article Reservation

**Delivery Copy**:
An immutable EPUB acknowledged by a Delivery Target. It is reader-facing output, not the authoritative State Store.
_Avoid_: Backup, state database

**Main Section**:
A top-level, ordered content placement within a Publication. Its label may name a person, subject, or any operator-chosen grouping.
_Avoid_: Profile, family member

**Subsection**:
An ordered content placement nested beneath a Main Section or another Subsection.
_Avoid_: Sub-profile

**Section**:
The general term for either a Main Section or Subsection. A Section may inherit or override its parent Policy Preset and Budget.
_Avoid_: Category

**Leaf Section**:
A Section with no nested Subsections. Sources attach only to Leaf Sections; a Main Section may itself be a Leaf Section.
_Avoid_: Feed section

**Source**:
An origin from which article metadata or journalistic content is discovered, such as an RSS or Atom feed and its linked publisher pages.
_Avoid_: Publisher

**Source Default Article Language**:
The BCP 47 language applied when a Source or its publisher item does not declare a more specific Article Language.
_Avoid_: Publication Language, language detection

**Article**:
An attributed piece of journalism discovered from a Source. An Article is distinct from feed metadata and editorial annotations.
_Avoid_: Feed item, story

**Article Language**:
The language of an Article’s publisher text, declared by its Source or publisher metadata and inherited by Article-specific Editorial Additions.
_Avoid_: Publication language, detected user language

**Publisher Link Brief**:
A selected, attributed reading item whose Source permits a publisher route but not reproduction of Article text. It is part of the finite Edition, not an inventory of acquired feed items. A Brief never occupies an Article Slot and is capped separately from Article content.
_Avoid_: Article, Source list, dead link

**Briefing Roll**:
The single aggregated Edition chapter carrying every Publisher Link Brief, ordered newest first across Sources and placed ahead of the Sections. It sits outside the Section tree and holds one navigation entry.
_Avoid_: Section, publisher link list, headline feed

**Source Presentation**:
Whether a Source contributes complete Articles or Publisher Link Briefs. It is derived from the Source's acquisition mode unless an operator overrides it.
_Avoid_: Acquisition mode, rights basis

**Canonical Rendition**:
The single complete rendering of an Article within an Edition, regardless of how many Sections consider it relevant.
_Avoid_: Master copy, duplicated article

**Primary Placement**:
The Section position containing an Article’s Canonical Rendition, chosen by strongest Section relevance with configured order as the tie-breaker.
_Avoid_: Owner, primary audience

**Section Pointer**:
A compact cross-reference at an Article’s ranked position in another relevant Section, linking to its Canonical Rendition without repeating its content.
_Avoid_: Duplicate, summary

**Content Revision**:
A timestamped observation of an Article’s normalized body. Multiple Content Revisions retain one Article identity and distinguish unchanged, minor, and material updates.
_Avoid_: New article, duplicate

**Delivered Revision**:
The Content Revision most recently delivered for an Article within one Publication. It is the reader-relative baseline for deciding whether accumulated changes are material.
_Avoid_: Latest fetch, global revision

**Correction Signal**:
An explicit publisher correction or retraction, or an operator instruction, that makes an Article eligible despite a smaller-than-material Content Revision.
_Avoid_: Modified timestamp, minor edit

**Discovery Provenance**:
The Sources, URLs, and source identifiers through which an Article was discovered. Equivalent content may carry multiple provenances while appearing once in an Edition.
_Avoid_: Duplicate source

**Article Reservation**:
A temporary claim that prevents an Article from entering another Edition while a validated Edition awaits delivery.
_Avoid_: Published article, lock

**Story Cluster**:
A non-selectable grouping of distinct Articles covering the same concrete real-world development. Its continuity never makes an unchanged Article eligible again.
_Avoid_: Merged article, topic

**Story Hub**:
A reader-facing navigation page for one Story Cluster, presenting its current Articles and compact metadata pointers to prior coverage without repeating old Article bodies.
_Avoid_: Article, shared section, archive

**Cluster Override**:
A private operator decision that corrects future Story Cluster membership while leaving delivered Editions unchanged.
_Avoid_: Feedback Signal, rewritten history

**Coverage Timeline**:
The Publication-specific history of distinct delivered Articles associated with a Story Cluster. It is separate from the Environment-wide identity and membership of the cluster.
_Avoid_: Article revision history, global cluster history

**Coverage Policy**:
A Section selection policy that prioritizes broad, plural current-affairs coverage. It is the inherited default when no policy is configured.
_Avoid_: Balanced mode, default mode

**Interest Policy**:
A Section selection policy that uses explicit operator preferences and feedback to adjust relevance.
_Avoid_: Personalized feed, filter mode

**Policy Preset**:
A named, reusable selection policy belonging to a Publication. A Section references a Policy Preset or inherits its parent’s choice; Coverage Policy is the root default.
_Avoid_: Mode, algorithm

**Source Weight**:
An explicit relative preference for selecting eligible Articles from a Source. It expresses editorial preference, not factual truth or viewpoint balance.
_Avoid_: Trust score, quality score

**Essential Coverage Slice**:
The highest-priority portion of a Section’s selection reserved for Articles identified by explicit, explainable importance signals.
_Avoid_: Objectively important news, front page

**Discovery Slice**:
A reserved portion of an Interest Policy selection that is not influenced by prior Feedback Signals, preserving exposure beyond established preferences.
_Avoid_: Random articles, filler

**Budget**:
The desired and permitted amount of Article content selected for a Publication or Section, constrained by every ancestor Budget.
_Avoid_: Quota, magic number

**Article Slot**:
One relevant Article counted within a Section Budget, whether represented by its Canonical Rendition or a Section Pointer. Ancestors count a repeated Article identity once across their subtree.
_Avoid_: Page, file

**Allocation Weight**:
A relative preference used to distribute available Article Slots among sibling Sections after essential coverage and feasible minimums.
_Avoid_: Priority, guarantee

**Effective Budget**:
The runtime Budget after ancestor constraints, minimum normalization, and deterministic redistribution are applied to configured intent.
_Avoid_: Rewritten config, hidden default

**Partial Edition**:
An Edition that remains below its target Article count after eligible candidates are exhausted but meets its Publication minimum and is still delivered.
_Avoid_: Failed Edition, broken digest

**Feedback Signal**:
An operator-provided positive or negative example that softly influences Interest Policy selection without muting content.
_Avoid_: Like, unlike, vote

**Mute Rule**:
An explicit instruction that excludes a Source or topic, distinct from a soft Feedback Signal.
_Avoid_: Dislike, negative feedback

**Publication Note**:
A minimal reader-facing notice in an Edition describing omitted content or degraded generation without internal diagnostic detail. Repeated failures are aggregated by Source, Section, and category.
_Avoid_: Error, stack trace

**Correction Notice**:
A required reader-facing notice that carries a publisher’s explicit correction or retraction for an Article previously delivered by the Publication, even when the current Article is not selected again.
_Avoid_: Publication Note, rewritten correction

**Diagnostic Event**:
A structured private observation emitted during a Run, carrying enough sanitized context to understand and reproduce an operational outcome.
_Avoid_: Log line, Publication Note

**Editorial Addition**:
Clearly labelled generated prose, such as an Article Summary, Revision Summary, or Main Section Overview, grounded in cited Articles and distinct from publisher journalism.
_Avoid_: Article, journalism, objective summary

**Editorial Boundary**:
The visible and semantic start and end of an Editorial Addition, separating generated prose from publisher text without relying on colour.
_Avoid_: Disclaimer paragraph, styling hint

**Editorial Proposal**:
A structured optional LLM response containing bounded ranking and clustering suggestions plus cited Editorial Additions. It has no authority until deterministic constraints and verification accept its parts.
_Avoid_: Final selection, generated Edition

**Verification Finding**:
A private classification of one factual claim as supported, unsupported, or uncertain against only the Articles supplied for verification.
_Avoid_: Confidence score, model opinion

**Editorial Gate**:
The boundary that admits independently verified Editorial Additions and bounded suggestions while omitting failures without weakening the deterministic Edition.
_Avoid_: LLM approval, confidence threshold

**Editorial Influence**:
The configured, measurable effect an accepted Editorial Proposal may have on ranking and Story Cluster suggestions. It may increase through operator decision as evidence improves but never weakens deterministic constraints.
_Avoid_: LLM authority, automatic trust

**Editorial Model**:
The configured LLM role that produces an Editorial Proposal.
_Avoid_: Generator, editor agent

**Verifier Model**:
A separately configured LLM role that independently produces Verification Findings for an Editorial Proposal or its single repair.
_Avoid_: Self-confidence, deterministic validator

**Model Pair**:
The pinned Editorial Model, Verifier Model, prompts, and schemas evaluated together. Quality and influence evidence never transfers implicitly to another pairing or version.
_Avoid_: Latest models, provider default

**Editorial Evaluation**:
The adversarial, operational, and human-review evidence accumulated for a Model Pair and editorial capability, used by an operator to change Editorial Influence.
_Avoid_: Self-assessment, automatic promotion

**LLM Cost Envelope**:
The Publication-specific call, token, and optional monetary limits reserved for a complete Editorial Proposal and verification loop.
_Avoid_: Article Budget, provider bill

**LLM Processing Policy**:
A Source-level permission distinguishing disabled, local-only, and explicitly allowed remote processing of acquired Article content.
_Avoid_: robots permission, Publication opt-in

**LLM Evidence Record**:
A size- and time-bounded private record of structured proposals, Verification Findings, usage, costs, and operator judgments for one Run, excluding Article bodies and full prompts.
_Avoid_: Diagnostic Event, training dataset

**Editorial Language**:
The language used for an Editorial Addition. An Article-specific addition uses its Article Language; translation requires a separate explicit capability.
_Avoid_: Publication language, implicit translation

**Revision Summary**:
An optional verified Editorial Addition that explains material differences between a Delivered Revision and a newer Content Revision, including why the Article appears again.
_Avoid_: Publisher correction, deterministic diff

**Run ID**:
A portable identifier created for one generation attempt before validation, correlating its output, Publication Notes, diagnostics, workflow run, and provider requests.
_Avoid_: GitHub Actions ID, build number
