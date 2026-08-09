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
- A summary is bounded to sixty words in total, counted across all its sentences and checked
  deterministically alongside the language gate. A repair that remains over the ceiling omits only
  that Article's summary; the Article keeps its publisher text either way.
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
