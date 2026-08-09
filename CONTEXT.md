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

**Article**:
An attributed piece of journalism discovered from a Source. An Article is distinct from feed metadata and editorial annotations.
_Avoid_: Feed item, story

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

**Discovery Provenance**:
The Sources, URLs, and source identifiers through which an Article was discovered. Equivalent content may carry multiple provenances while appearing once in an Edition.
_Avoid_: Duplicate source

**Article Reservation**:
A temporary claim that prevents an Article from entering another Edition while a validated Edition awaits delivery.
_Avoid_: Published article, lock

**Story Cluster**:
A group of Articles from one or more Sources that cover the same real-world development.
_Avoid_: Merged article

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

**Diagnostic Event**:
A structured private observation emitted during a Run, carrying enough sanitized context to understand and reproduce an operational outcome.
_Avoid_: Log line, Publication Note

**Editorial Addition**:
Clearly labelled generated prose, such as an Article Summary or Main Section Overview, grounded in cited Articles and distinct from publisher journalism.
_Avoid_: Article, journalism, objective summary

**Run ID**:
A portable identifier created for one generation attempt before validation, correlating its output, Publication Notes, diagnostics, workflow run, and provider requests.
_Avoid_: GitHub Actions ID, build number
