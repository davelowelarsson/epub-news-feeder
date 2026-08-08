# Research 0003: EPUB construction and validation toolchain

- Issue: [#3 — Choose the EPUB construction and validation toolchain](https://github.com/davelowelarsson/epub-news-feeder/issues/3)
- Researched: 2026-08-08
- Status: recommendation

## Decision

Build the canonical EPUB 3.3 Edition with a small, standards-native Python writer owned by this project. The concrete runtime stack is the project's supported Python, a pinned `lxml.etree` for namespace-aware XML/XHTML construction and serialization, and Python's standard-library `zipfile` for the container. The writer should turn the already-structured `Edition` into XHTML, one shared navigation tree, an OPF package document, embedded resources, and finally a deterministic ZIP container. Do not concatenate unescaped source text into markup.

Validate every packaged Edition with the official EPUBCheck 5.3.0 command-line distribution. Pin both its version and release-asset SHA-256 (`6c07e68584b2e2ce2f89fe06e1246dfead3eb36b46b340e7d93524f29dcff6c5`) and run with `--failonwarnings`. Keep Calibre's viewer/editor as a local human-inspection tool, not a runtime or CI dependency.

The delivered source of truth remains a standards-compliant, reflowable `.epub`. For Kobo device QA, also sideload an identically sourced `.kepub.epub` test copy as Kobo documents. Do not make KEPUB production a requirement initially. If device testing proves that Kobo-only features such as image zoom, pop-up footnotes, or reading statistics are required, add a separately pinned `kepubify` post-processing target and validate its output again. Never replace the canonical EPUB with the derived KEPUB.

This choice deliberately accepts a little project-owned packaging code. In return it avoids an AGPL runtime library, opaque conversion transforms, desktop software in scheduled operation, and timestamps or identifiers that silently make equal builds differ.

## Verified facts

The statements in this section are facts from specifications, official project repositories, or vendor documentation. Evaluative conclusions are kept in later sections.

### EPUB 3.3 requirements that control the design

- An EPUB is a ZIP-based OCF container. Its `mimetype` entry has to be first, contain exactly `application/epub+zip`, and be stored without compression or an extra ZIP field. Other entries may be stored or Deflate-compressed. [EPUB 3.3, OCF ZIP requirements](https://www.w3.org/TR/epub-33/#sec-zip-container-mime)
- Every publication needs a conforming package document and navigation document. Publication resources used for rendering have to appear in the package manifest; the spine declares default reading order. [EPUB 3.3, publication conformance](https://www.w3.org/TR/epub-33/#sec-epub-conformance) and [package document](https://www.w3.org/TR/epub-33/#sec-package-doc)
- The package has to declare exactly one manifest item with the `nav` property. The navigation document has to contain exactly one `toc` navigation element. It may also be placed in the spine and displayed as the reader-facing table of contents. [EPUB 3.3, manifest resource properties](https://www.w3.org/TR/epub-33/#sec-item-resource-properties) and [navigation requirements](https://www.w3.org/TR/epub-33/#sec-nav-def-model)
- Required package metadata is `dc:identifier`, `dc:title`, `dc:language`, and exactly one UTC `dcterms:modified` value in `YYYY-MM-DDThh:mm:ssZ` form. The selected identifier has to identify one publication and be referenced by the package's `unique-identifier` attribute. [EPUB 3.3, package metadata](https://www.w3.org/TR/epub-33/#sec-pkg-metadata) and [last modified date](https://www.w3.org/TR/epub-33/#sec-last-modified-date)
- JPEG, PNG, GIF, SVG, and WebP images and CSS are EPUB 3.3 core media types. All publication resources still need correct manifest entries and media types. CSS must not set `direction` or `unicode-bidi`; UTF-8 is recommended. [EPUB 3.3, core media types](https://www.w3.org/TR/epub-33/#sec-core-media-types) and [CSS requirements](https://www.w3.org/TR/epub-33/#sec-css)
- EPUB 3.3 recommends identifying a cover image with the manifest `cover-image` property. [EPUB 3.3, manifest resource properties](https://www.w3.org/TR/epub-33/#sec-item-resource-properties)
- EPUB 3 publications should conform to EPUB Accessibility. Its techniques include meaningful alternative text for informative images and empty alternative text for purely presentational images. [EPUB Accessibility 1.1](https://www.w3.org/TR/epub-a11y-11/) and [EPUB Accessibility Techniques 1.1, descriptions](https://www.w3.org/TR/epub-a11y-tech-11/#sec-desc)

### Construction candidates

| Candidate | Verified capabilities and constraints |
| --- | --- |
| Project-owned Python writer | Python's standard library exposes ZIP entry ordering, compression method, entry metadata, and bytes through [`zipfile`](https://docs.python.org/3/library/zipfile.html). [`lxml.etree`](https://lxml.de/apidoc/lxml.etree.html) exposes namespace-aware XML trees, XML serialization, and canonicalization; lxml is BSD-licensed and its underlying libxml2/libxslt are MIT-licensed. [Official license statement](https://lxml.de/#license) The EPUB structure itself is specified XML/XHTML/CSS plus ZIP; no conversion engine is required by EPUB 3.3. The tradeoff is a pinned native-backed wheel rather than an EPUB-specific dependency. |
| [EbookLib 0.20](https://pypi.org/project/EbookLib/0.20/) | Production/stable Python package that reads and writes EPUB 2/3 and exposes metadata, cover, ToC, spine, image, and CSS APIs. It is AGPL-3.0-or-later. Its v0.20 writer accepts an `mtime` option for `dcterms:modified`, but otherwise uses `datetime.now()` and lets `zipfile.writestr` assign ZIP timestamps, so default output is time-dependent. [Writer source](https://github.com/aerkalov/ebooklib/blob/v0.20/ebooklib/epub.py) |
| [python-epub3](https://pypi.org/project/python-epub3/) | MIT library with direct EPUB 3 metadata, manifest, spine, and resource access. Its own project page says it is under development and not to use it in production. |
| [epublib 0.1.7](https://pypi.org/project/epublib/0.1.7/) | MIT, Python 3.13+, direct metadata/manifest/spine/nav/resource API. PyPI classifies it as alpha; its first public 0.1 release was in September 2025. |
| [pypublib 0.1.3](https://pypi.org/project/pypublib/0.1.3/) | MIT, Python 3.10+, direct chapter/image/CSS/metadata/nav/NCX API. The release was published in July 2026 and has one listed maintainer. Its `validate_book` helper is not the official EPUB conformance checker. |
| [Pandoc](https://pandoc.org/MANUAL.html#epubs) | External GPL-2.0-or-later executable with an EPUB 3 writer, CSS, cover image, embedded font, metadata, and split-level options. When no identifier is supplied it generates a random UUID. Its general-purpose AST and writer convert source markup rather than accepting this project's complete Edition/package model directly. [Official copyright](https://github.com/jgm/pandoc/blob/main/COPYRIGHT) |
| [Calibre `ebook-convert`](https://manual.calibre-ebook.com/generated/en/ebook-convert.html) | External GPL-3.0 application with EPUB and KEPUB output, a Kobo profile, image resizing, CSS transforms, structure detection, metadata, and ToC generation. Its documented option defaults vary with the input/output formats; conversion can split flows and rewrite content. [Official repository and license](https://github.com/kovidgoyal/calibre) |

### Validation and reader inspection

- EPUBCheck is the official W3C conformance checker. Release 5.3.0 is the production release for EPUB 3.3, is BSD-3-Clause, and is available as a standalone Java distribution. [Official repository](https://github.com/w3c/epubcheck) and [v5.3.0 release](https://github.com/w3c/epubcheck/releases/tag/v5.3.0)
- EPUBCheck normally exits nonzero on errors. `--failonwarnings` also makes warnings fail, and `--json` emits a machine-readable report. Validating the packaged EPUB exercises more checks than validating individual expanded files. [EPUBCheck command-line documentation](https://github.com/w3c/epubcheck/wiki/Running)
- Calibre provides `ebook-viewer` and an editor with live HTML/CSS preview, a book check, reports, and link inspection. The editor handles EPUB and KEPUB. [Viewer](https://manual.calibre-ebook.com/viewer.html), [editor](https://manual.calibre-ebook.com/edit.html), and [CLI index](https://manual.calibre-ebook.com/generated/en/cli-index.html)
- Kobo says a file can validate cleanly and still require testing on Kobo software/hardware. Its guidance asks for at least an eReader (or Desktop) and a phone/tablet test. For a reflowable eReader sideload that exercises Kobo rendering, it says to change the suffix to `.kepub.epub`; such sideloads can lose bookmarks/notes and cover thumbnails. [Kobo validation and testing](https://kobowritinglife.zendesk.com/hc/en-us/articles/360058976112-Validating-and-Testing-Your-eBooks)
- Kobo recommends a navigable ToC and zero validation errors. [Kobo EPUB best practices](https://kobowritinglife.zendesk.com/hc/en-us/articles/360059385611-EPUB-Best-Practices)
- `kepubify` is an independent, MIT-licensed Go tool rather than a Kobo product. It adds the Kobo spans used by enhanced KEPUBs and documents Kobo-only behavior such as reading statistics, image zoom, and pop-up footnotes. Its latest published release is 4.0.4 from 2022. [Project documentation](https://pgaskin.net/kepubify/) and [v4.0.4 source](https://github.com/pgaskin/kepubify/tree/v4.0.4)

## Analysis and recommendation

Everything in this section is an inference from the verified facts and this project's stated needs.

### Why the project-owned writer wins

The Edition is already a typed, ordered publication: Publication metadata, Main Sections, nested Subsections, Articles, Publication Notes, and a Run ID. A format converter would have to infer structure which the application already knows. A narrow writer can preserve that structure directly and deterministically.

The required scope is small:

1. Render a finite set of XHTML documents from trusted templates and sanitized Article content.
2. Generate `nav.xhtml` from the same ordered tree used to generate the spine.
3. Generate `content.opf` from the same resource registry.
4. Add `META-INF/container.xml`, assets, and `mimetype` to a deterministic ZIP.

This is less surface area than adapting Pandoc or Calibre and then testing all of their transformations. It is more implementation work than EbookLib, but avoids EbookLib's AGPL license and makes timestamps, filenames, resource order, identifiers, and serialization explicit. `lxml` adds native code and wheel/platform pinning, but it is permissively licensed and is likely reusable by the acquisition/sanitization boundary. EPUBCheck remains the conformance authority either way, so a library does not remove the need for fixture and integration tests.

Do not treat this conclusion as legal advice. If the project later chooses AGPL-compatible licensing after review, EbookLib 0.20 becomes a reasonable fallback. It must still receive an explicit UTC `mtime` and be deterministically repacked or patched because its default writer records build time in both metadata and ZIP entries.

The newer MIT direct libraries are promising but currently shift core publishing risk onto alpha or very new packages. Re-evaluate them after sustained releases and fixture-based proof; do not make them the first production boundary.

Pandoc is justified when Markdown/Pandoc AST is the canonical authoring model. Calibre conversion is justified when broad input-format conversion is the product. Neither is true here. Their executables, transformation behavior, update cadence, and packaging cost are unnecessary in the scheduled runtime.

### Runtime construction contract

Use one immutable resource registry as the source for both the OPF manifest and ZIP entries. Reject duplicate IDs, duplicate paths, absolute paths, `..` path segments, unknown media types, missing referenced resources, and resources not represented in the manifest before writing.

Suggested container layout:

```text
mimetype
META-INF/container.xml
EPUB/content.opf
EPUB/nav.xhtml
EPUB/styles/edition.css
EPUB/text/title.xhtml
EPUB/text/note-*.xhtml
EPUB/text/article-*.xhtml
EPUB/images/*
```

Generate exactly one nested navigation tree from the Publication's ordered Sections and Articles. Use it for `nav.xhtml`; flatten the same tree for the OPF spine. Put `nav.xhtml` in the spine so it is also a visible contents page. Include legacy NCX only if real-device testing identifies an unsupported target; it is not required for EPUB 3.

Package metadata:

- Required: a stable Edition `dc:identifier`, Publication title, BCP 47 language, and explicit UTC build instant as `dcterms:modified`.
- Recommended: `dc:creator`, Edition publication date, `dc:publisher`, `dc:description`, and rights statement when known.
- Derive the identifier deterministically from stable Publication identity plus Edition publication instant and Run ID. Pass both identifier and modified time into the writer; never generate them inside serialization.
- Keep Article attribution, original URL, original publication time, and Source label in the Article XHTML. Package-level authorship must not imply that the generator authored the journalism.

Content and styling:

- Use reflowable XHTML with semantic headings, paragraphs, lists, quotations, `lang`, and source links. Do not encode print page numbers.
- Sanitize acquired HTML before it reaches the writer. Serialize parsed nodes, not raw string interpolation. Disable scripts and do not fetch resources while packaging.
- Embed every rendering dependency. Keep ordinary outbound article links as links; they are not publication resources.
- Prefer JPEG or PNG for initial Kobo compatibility even though EPUB 3.3 also defines WebP and SVG as core image types. Normalize orientation, cap decoded pixel count and encoded bytes, preserve aspect ratio, and supply meaningful `alt` text or `alt=""` as appropriate.
- Use one small UTF-8 stylesheet with relative units, no fixed viewport, no forced body font, no `direction`/`unicode-bidi`, and no essential information encoded only by color. Test font-size changes, narrow screens, and dark mode. Avoid embedded fonts initially.
- Mark the cover image with `properties="cover-image"`; include a simple cover XHTML page only if the reading experience needs it.

### Deterministic packaging

For equal inputs and tool versions, output bytes should be equal:

- Sort or explicitly order every resource; never depend on set, filesystem, or network completion order.
- Build generated documents with `lxml.etree`; serialize with one fixed XML-mode, UTF-8 configuration and canonicalize declarations, namespace prefixes, line endings, and trailing newlines.
- Give every `ZipInfo` an explicit timestamp derived from `SOURCE_DATE_EPOCH` (clamped to ZIP's 1980 minimum), fixed permissions, fixed `create_system`, no extra/comment fields, and an explicit compression method/level.
- Write `mimetype` first with `ZIP_STORED`; use a fixed Deflate level for other entries.
- Make Edition timestamp and Run ID explicit build inputs. Exclude host paths, current time, random UUIDs, and tool banners.
- Pin Python dependencies with hashes and pin EPUBCheck's binary digest. A scheduled dependency-update change should rebuild the golden fixture and show the artifact diff.

Bit reproducibility does not mean two different Editions share an identifier or modified time. It means rebuilding the same Edition input yields the same bytes.

### Validation stack

Use three layers:

1. **Fast Python tests:** XML/XHTML parses; IDs and paths are unique; all local references resolve; manifest, spine, and ZIP registries agree; the ToC and spine derive from the same tree; required metadata is exact; `mimetype` ZIP invariants hold.
2. **Packaged-artifact gate:** EPUBCheck 5.3.0 with `--failonwarnings`, saving JSON diagnostics as a private CI artifact only when the repository's privacy design permits it. Do not weaken message severities. Upgrade EPUBCheck deliberately, not through an unpinned `latest` download.
3. **Human compatibility QA:** Calibre viewer/editor on each meaningful template/CSS change, then Kobo Desktop/eReader and a Kobo phone/tablet app for release candidates. A green EPUBCheck run proves conformance, not rendering parity.

Calibre may be installed on a developer workstation for inspection. It must not be invoked by Edition generation, delivery, or the default CI gate.

### Kobo and KEPUB boundary

The canonical EPUB should stay vendor-neutral. For normal Kobo testing, duplicate it under a `.kepub.epub` name exactly as Kobo's own sideload instructions specify. This selects Kobo-style rendering but does not add enhanced KEPUB spans.

Only add `kepubify` if acceptance testing demonstrates a need for enhanced KEPUB behavior. In that case:

- pin v4.0.4 and its per-platform binary digest;
- create `Edition.epub` first, then derive `Edition.kepub.epub`;
- run EPUBCheck against both;
- keep the plain EPUB for debugging and other readers;
- add Kobo device assertions for ToC, footnotes, images, highlighting, page/chapter progress, and typography;
- revisit the age and maintenance posture of the pinned 2022 converter before adopting it.

Renaming for QA and converting with `kepubify` are different operations. The first follows Kobo's preview workflow; the second mutates content for enhanced reader features.

## Failure modes and controls

| Failure | Detection | Control |
| --- | --- | --- |
| `mimetype` is compressed, padded, or not first | ZIP invariant unit test; EPUBCheck | Dedicated first `ZipInfo`, exact ASCII bytes, `ZIP_STORED`, no extra field |
| Malformed or unsafe acquired HTML | Sanitizer/parser test; XML parse; EPUBCheck | Parse and allow-list before templates; never interpolate raw markup or package scripts |
| Missing, duplicate, or case-mismatched paths/IDs | Resource-registry unit test; EPUBCheck | One normalized registry; reject absolute/parent paths and duplicates before writing |
| Manifest, ToC, and spine disagree | Cross-model integration test; EPUBCheck; reader navigation | Derive all three from one ordered Edition tree |
| Wrong media type or an omitted image/CSS file | Reference walk; manifest/ZIP equality test; EPUBCheck | Sniff approved image types, assign explicit media types, embed dependencies |
| Non-reproducible ZIP or metadata | Build twice and `cmp`; inspect ZIP entries | Explicit ID/time inputs, fixed serialization, sorted resources, fixed ZIP metadata |
| Local time is falsely marked UTC | Metadata assertion; EPUBCheck format check | Convert once to UTC and format with `Z`; never call `now()` inside the writer |
| Huge/decompression-bomb image exhausts CI or reader | Image limits before decode; fixture test | Byte and pixel ceilings, decoder warnings as failures, resize outside packaging |
| CSS looks valid but overrides reader preferences or breaks Kobo | Calibre and Kobo matrix | Conservative reflowable CSS, relative units, no forced fonts/sizes/colors |
| EPUBCheck release changes results unexpectedly | Pinned digest; explicit update PR | Versioned tool cache and golden-fixture comparison |
| Plain EPUB and KEPUB behavior diverge | Device checklist for both artifacts | Plain EPUB is canonical; vendor output is derived and independently validated |
| Calibre/Pandoc silently rewrites content | Artifact diff if ever evaluated | Keep both out of runtime; require fixture proof before introducing either |

## Concrete acceptance commands

These commands define the implementation target. The future CLI may wrap them, but it must preserve their observable outcomes.

### Build twice and prove archive determinism

```sh
export SOURCE_DATE_EPOCH=1786147200
uv run python -m epub_news_feeder build \
  --fixture tests/fixtures/edition/minimal.json \
  --output build/edition-a.epub
uv run python -m epub_news_feeder build \
  --fixture tests/fixtures/edition/minimal.json \
  --output build/edition-b.epub
cmp build/edition-a.epub build/edition-b.epub
```

`cmp` must exit 0. A Python archive test should additionally assert the first entry and exact media type without relying on platform-specific `zipinfo` output:

```sh
uv run python - <<'PY'
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

path = Path("build/edition-a.epub")
with ZipFile(path) as archive:
    first = archive.infolist()[0]
    assert first.filename == "mimetype"
    assert first.compress_type == ZIP_STORED
    assert not first.extra
    assert archive.read(first) == b"application/epub+zip"
PY
```

### Run project and EPUB integration tests

```sh
uv run pytest tests/epub
```

The suite must include a nested Main Section/Subsection ToC, non-ASCII text, an attributed Article link, a Publication Note with Run ID, CSS, a cover, one informative image, one decorative image, and a deliberately broken fixture that proves the validator gate fails.

### Install the pinned official validator and fail on warnings

```sh
mkdir -p .cache/epubcheck
curl --fail --location \
  --output .cache/epubcheck/epubcheck-5.3.0.zip \
  https://github.com/w3c/epubcheck/releases/download/v5.3.0/epubcheck-5.3.0.zip
printf '%s  %s\n' \
  6c07e68584b2e2ce2f89fe06e1246dfead3eb36b46b340e7d93524f29dcff6c5 \
  .cache/epubcheck/epubcheck-5.3.0.zip | shasum -a 256 -c -
unzip -q -o .cache/epubcheck/epubcheck-5.3.0.zip -d .cache/epubcheck
java -jar .cache/epubcheck/epubcheck-5.3.0/epubcheck.jar \
  build/edition-a.epub \
  --failonwarnings \
  --json build/epubcheck.json
```

The checksum and validator commands must exit 0. CI should use a pinned LTS Java runtime and a commit-pinned setup action. Do not download `latest` and do not rely on an unversioned system EPUBCheck.

### Inspect locally in Calibre

```sh
ebook-meta build/edition-a.epub
ebook-viewer build/edition-a.epub
ebook-edit build/edition-a.epub
```

Check the visible ToC and nested navigation, cover, title/language/creator metadata, source links, image alternatives, font resizing, narrow layout, and dark theme. The last two commands are interactive and are release/template-change checks, not CI gates.

### Exercise Kobo rendering

```sh
cp build/edition-a.epub build/edition-a.kepub.epub
```

Sideload that file following Kobo's linked instructions. Test on an eReader or Kobo Desktop and on one phone/tablet app. If enhanced KEPUB becomes a requirement, the optional target is:

```sh
kepubify --output build/kobo/ build/edition-a.epub
java -jar .cache/epubcheck/epubcheck-5.3.0/epubcheck.jar \
  build/kobo/edition-a_converted.kepub.epub --failonwarnings
```

## Revisit triggers

Re-open this decision if any of the following becomes true:

- the canonical authoring model changes to Markdown/Pandoc AST;
- broad ebook input conversion becomes a product feature;
- the project adopts AGPL-compatible licensing and maintaining the narrow writer costs more than adapting EbookLib;
- a mature permissive Python library passes the full fixture and reproducibility suite;
- Kobo delivery requires enhanced KEPUB features rather than standard EPUB handoff;
- EPUB 3.4 or a newer production EPUBCheck changes the required package contract.
