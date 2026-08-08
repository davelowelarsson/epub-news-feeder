# EPUB News Feeder

The publication domain for assembling private, finite news digests from public and operator-configured sources.

## Language

**Publication**:
A configured recurring news product with its own structure, selection rules, budgets, and delivery targets.
_Avoid_: Digest configuration, family feed

**Edition**:
One generated EPUB belonging to a Publication and identified by its publication time and Run ID.
_Avoid_: Build, artifact, issue

**Main Section**:
A top-level, ordered content placement within a Publication. Its label may name a person, subject, or any operator-chosen grouping.
_Avoid_: Profile, family member

**Subsection**:
An ordered content placement nested beneath a Main Section or another Subsection.
_Avoid_: Sub-profile

**Section**:
The general term for either a Main Section or Subsection. A Section may inherit or override its parent selection policy.
_Avoid_: Category

**Source**:
An origin from which article metadata or journalistic content is discovered, such as an RSS or Atom feed and its linked publisher pages.
_Avoid_: Publisher

**Article**:
An attributed piece of journalism discovered from a Source. An Article is distinct from feed metadata and editorial annotations.
_Avoid_: Feed item, story

**Story Cluster**:
A group of Articles from one or more Sources that cover the same real-world development.
_Avoid_: Merged article

**Coverage Policy**:
A Section selection policy that prioritizes broad, plural current-affairs coverage. It is the inherited default when no policy is configured.
_Avoid_: Balanced mode, default mode

**Interest Policy**:
A Section selection policy that uses explicit operator preferences and feedback to adjust relevance.
_Avoid_: Personalized feed, filter mode

**Feedback Signal**:
An operator-provided positive or negative example that softly influences Interest Policy selection without muting content.
_Avoid_: Like, unlike, vote

**Mute Rule**:
An explicit instruction that excludes a Source or topic, distinct from a soft Feedback Signal.
_Avoid_: Dislike, negative feedback

**Publication Note**:
A minimal reader-facing notice in an Edition describing omitted content or degraded generation without internal diagnostic detail.
_Avoid_: Error, stack trace

**Run ID**:
A portable identifier correlating an Edition and its Publication Notes with private operational diagnostics.
_Avoid_: GitHub Actions ID, build number
