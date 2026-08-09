# Reader Experience Contract

This document records the reader-visible behavior expected from the local EPUB MVP. Domain terms
are defined in `CONTEXT.md`.

## Publisher text and Editorial Additions

- An Article-specific Editorial Addition uses the Article Language. Mixed-language Editions may
  therefore contain Swedish and English summaries beside matching publisher text.
- Every local editorial batch contains one Article. English and Swedish output is also checked by a
  deterministic language gate before verifier acceptance; one unstable or unverifiable response
  therefore omits only that Article's summary.
- Generated prose has a visible and semantic Editorial Boundary with explicit start and end. The
  publisher text begins under its own label and must not appear to continue the summary.
- The short disclosure explaining generation and independent verification appears once in Edition
  end matter, not once per Article.
- Every generated sentence keeps descriptive publisher citations.
- A summary must add orientation or compression. A proposal that substantially repeats the
  publisher lead without adding reader value is omitted.

## Publisher content normalization

- Publisher paragraph boundaries remain paragraph boundaries in XHTML. Normalization must not join
  the end of one paragraph directly to the start of the next.
- Feed or page chrome such as “Read full article” and “Comments” is not publisher Article text and
  must not enter the Canonical Rendition.
- A feed fragment that advertises “Read full article” is not accepted as a complete Article when an
  eligible full-page route is available.
- Unsupported diagram source, including raw Mermaid syntax, must not be rendered as prose. Until a
  separately accepted diagram/image policy exists, it is omitted while the canonical publisher
  route remains available.

## Body block rendering

Acquisition classifies every unit of an Article's publisher body into a Body Block kind; rendering
decides what to do with each kind. The two representations deliberately disagree on one thing: the
plain-text body excludes diagram source, while the Body Blocks include it, so diagram source never
reaches the revision hash, rule matching, or an LLM prompt, yet survives for a future renderer.

- A `paragraph` block renders as a paragraph.
- A `quote` block renders as a block quotation.
- Consecutive `list` blocks render as one unordered list; an isolated `list` block renders as a
  single-item list.
- A `code` block renders monospace, one size step down, with `white-space: pre-wrap` and a hanging
  indent on continuation lines, because e-ink cannot scroll horizontally and a long line must wrap
  rather than clip.
- A `diagram` block is retained in the data and omitted at render, preserving today's reader-visible
  behavior while keeping the source available to a future renderer.
- An unrecognised block kind is omitted; it is never rendered as text.

## Publisher Link Briefs

- A Publisher Link Brief is a selected reading item with headline, source, byline/date metadata,
  and a descriptive publisher route.
- It does not reproduce or summarize publisher text when the Source eligibility route permits only
  linking. This is intentional, not an acquisition failure.
- Unselected metadata candidates never appear in the Edition or durable State.

## Reader-generated copy notices

Reader software may append copy/export notices such as “Excerpt From” and “This material may be
protected by copyright.” Those strings are not Edition content and must not be duplicated by the
generator.

## Images and diagrams

Image inclusion remains an explicit future decision. See GitHub issue #38. Article-body eligibility
does not imply permission to reuse publisher images.

## Edition cover

The Edition cover is a separate product-owned design surface, not a publisher Article image. The
decision in GitHub issue #39 must define a reusable visual system before implementation:

- The publication identity and Edition date remain legible at Kobo library-thumbnail size.
- The design works in color and grayscale on e-ink; color alone never carries meaning.
- A deterministic template produces a stable daily identity without embedding remote tracking or
  publisher media.
- The cover has useful accessibility text and uses the EPUB 3 `cover-image` manifest property.
- The full-screen cover stays restrained and readable rather than imitating a web-news front page.

Publisher images may not enter the cover unless a separate Source-specific media decision permits
that exact use.
