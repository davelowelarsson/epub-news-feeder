# Reader Experience Contract

This document records the reader-visible behavior expected from the local EPUB MVP. Domain terms
are defined in `CONTEXT.md`.

## Language of labels versus language of text

- Every label the generator writes to the reader uses the **Publication Language**: navigation, the
  Edition overview, notes and corrections headings, bylines, source and date lines, rights lines,
  the publisher-article heading and publisher route, cross-reference and Story Hub headings, the
  update notice, and end matter. A label is the generator speaking to the reader, not the publisher
  speaking, and the reader is one person reading in one language.
- Two carve-outs follow the **Article Language** instead. Editorial Addition prose, because a
  summary describes the reporting it summarizes. And the `lang` and `xml:lang` attributes on
  publisher text and on the summary aside, because those drive hyphenation, justification and
  text-to-speech and must describe the language of the text they wrap rather than the Edition's
  chrome.
- Whether an Article changed materially since the reader's last Edition is a fact supplied to the
  generator; the wording of the notice is the generator's own and is localized with every other
  label.
- A Publication Language outside the supported set falls back to English chrome **silently**. This
  is deliberate for now, not an oversight: label translations must exist before a third Publication
  Language is configured. See GitHub issue #50.

## Publisher text and Editorial Additions

- An Article-specific Editorial Addition uses the Article Language. Mixed-language Editions may
  therefore contain Swedish and English summaries beside matching publisher text.
- Every local editorial batch contains one Article. English and Swedish output is also checked by a
  deterministic language gate before verifier acceptance; one unstable or unverifiable response
  therefore omits only that Article's summary.
- The Editorial Model is instructed to aim for about sixty words per summary, never to exceed one
  hundred and twenty, and never to mirror or parrot the publisher text. A hard ceiling of one
  hundred and twenty words is then enforced deterministically alongside the language gate, counted
  across all of a summary's sentences. The instructed target and the enforced ceiling are
  deliberately different: overshooting a stated target is tolerable, retelling the Article is not,
  and enforcing a limit the model was never told produces omissions rather than shorter summaries.
  A repair that remains over the ceiling omits only that Article's summary; the Article keeps its
  publisher text either way.
- Generated prose has a visible and semantic Editorial Boundary with explicit start and end. The
  publisher text begins under its own label and must not appear to continue the summary.
- The short disclosure explaining generation and independent verification appears once in Edition
  end matter, not once per Article.
- The end matter names the Sources in the Edition whose publishers do not permit generated
  summaries, stated factually. A silently absent summary is otherwise indistinguishable from a
  failed one, and a deliberate policy outcome would read as a defect. The end matter appears for an
  excluded Source even when no summary was generated at all. There is no per-Article marker: that
  would put chrome on every page of an affected Source.
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

- A Publisher Link Brief is a headline and a route back to its publisher. It does not reproduce or
  summarize publisher text when the Source eligibility route permits only linking. This is
  intentional, not an acquisition failure.
- **A Brief is a rights outcome, never a failure outcome.** A failed full-text acquisition is
  omitted and aggregated into a Publication Note; it never quietly demotes to a headline, because
  the reader could not tell the difference.
- Briefs are capped separately from Articles and never consume an Article Slot, so a headline can
  never displace journalism. They cannot satisfy the Publication minimum either: an Edition of pure
  headlines is a notification, not a reading product, and is not delivered.
- Every Brief gathers into one **In Brief** chapter placed after Edition notes and Corrections and
  before the Sections — anything the reader is owed comes first, then the skim eases them into the
  reading. The chapter is omitted entirely when there are no Briefs.
- Briefs are ordered newest first across all Sources. Grouping by Source would re-fragment what
  aggregation just fixed. Selection is round-robin across Sources so one publisher cannot fill the
  roll.
- Each entry is a plain-text headline with one sub-line carrying the source name and the
  publication time. **The publisher link sits on the source name, not the headline**, so a reader
  checking a headline has an obvious place to press and the headline itself stays unadorned. No
  byline, no rights line, no kind label, no call to action.
- The chapter has a single table-of-contents entry. Listing every Brief in the contents is the same
  chrome the chapter exists to remove.
- Mute Rules apply to Briefs; a muted topic reaching the reader as a headline is exactly what a
  Mute Rule exists to prevent. Relevance ranking, Source Weight, Essential Coverage and Feedback
  Signals do not apply.
- Unselected metadata candidates never appear in the Edition or durable State.
- A delivered Brief never comes back. Its durable identity is the unkeyed hash of its normalized
  canonical URL, so a reworded headline on the same report stays suppressed while a genuinely new
  report — a different canonical URL — is unaffected. Suppression is permanent and scoped per
  Publication; it is applied before selection, and the durable State carries only the hash, the
  Source, and the publication and delivery times — no headline, no URL, no body.

## Reader-generated copy notices

Reader software may append copy/export notices such as “Excerpt From” and “This material may be
protected by copyright.” Those strings are not Edition content and must not be duplicated by the
generator.

## Images and diagrams

Image inclusion remains an explicit future decision. See GitHub issue #38. Article-body eligibility
does not imply permission to reuse publisher images.

## Edition cover

The Edition cover is a separate product-owned design surface, not a publisher Article image. Every
Edition carries a restrained typographic SVG cover carrying, in the Publication Language:

- the Publication title, set large enough to survive Kobo library-thumbnail size and wrapped on
  word boundaries rather than relying on renderer text layout;
- the Edition date;
- the Article count and, when the Edition has any, the Brief count. Counts are derived from the
  Edition being built, so they cannot disagree with its contents. The title and date already
  establish when an Edition is from, so an old cover reads as old rather than thin.

Constraints the cover holds to:

- The design works in colour and greyscale on e-ink; colour never carries meaning.
- Deterministic — identical title, language, date and counts produce identical bytes.
- No imagery, no publisher media, no generative or date-derived mark, no embedded font, and no
  fetchable reference of any kind.
- Useful accessibility text as an SVG title and description with an image role, and the EPUB 3
  `cover-image` manifest property. The cover is an image item, never a reading document.

SVG is chosen knowingly: `cover-image` applies to an image item, so a typographic cover still has
to be an image, and Kobo's SVG font handling is inconsistent. If the device rejects SVG text,
converting text to paths is the identified fallback — it keeps determinism and removes the device
font dependency without adding a rasteriser. See GitHub issue #39.

Publisher images may not enter the cover unless a separate Source-specific media decision permits
that exact use.
