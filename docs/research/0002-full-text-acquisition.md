# Research 0002: full-text acquisition across reality-check feeds

**Issue:** [#2 — Prove full-text acquisition across the reality-check feeds](https://github.com/davelowelarsson/epub-news-feeder/issues/2)
**Observed:** 2026-08-08 (read-only HTTPS probes; one feed fetch and up to three linked-page samples per reachable publisher)
**Scope:** acquisition reliability, not a grant of republication rights. `robots.txt` and Content Signals are access signals, not a substitute for an operator's rights review.

## Documented facts

The probes used a descriptive, non-browser User-Agent, `curl -L --compressed --max-time 30`, and XML inspection. They did not authenticate, submit forms, defeat an access control, or download audio/video. Counts and payload lengths are snapshots, not a promise about future feed contents.

| Source | Discovery and body observed | Linked page / canonical | Metadata and lead-image evidence | Access result |
| --- | --- | --- | --- | --- |
| [David Lowe Larsson](https://davidlowelarsson.com/rss.xml) | RSS 2.0 returned **11** items. Every item had `content:encoded`; the first was 8,966 characters / 14 `<p>` elements (versus its 140-character description). | The first linked page returned 200 and its canonical was the feed URL, [this post](https://davidlowelarsson.com/posts/essay-ai-code-ownership/). The same distinctive text occurs in both feed and page. | The page has description, `og:title`, `og:description`, `og:image`, and `BlogPosting` JSON-LD. The feed has four `media:content` image records overall (and no RSS enclosures); some bodies contain inline images. | Feed and sampled pages returned 200. [`robots.txt`](https://davidlowelarsson.com/robots.txt) allows `User-agent: *`, but also says `ai-train=no,use=reference`; it separately blocks several named AI bots. |
| [Ars Technica](https://feeds.arstechnica.com/arstechnica/index) | RSS 2.0 returned **20** items. The first `content:encoded` was 978 characters / five paragraphs and ends in **“Read full article”**; it is a preview, not a complete body. Ars’ own [RSS directory](https://arstechnica.com/rss-feeds/) publishes this feed and separately identifies subscriber full-text feeds. | The three sampled linked articles returned 200. The first has a self-canonical, [this article](https://arstechnica.com/space/2026/08/the-first-self-driving-vehicle-on-mars-has-proven-to-be-a-smashing-success/), and an HTML `article` / `.post-content` body. | Feed entries expose Media RSS `content`, `thumbnail`, credit, and caption; the first has a 1152×648 image. The page also exposes Open Graph image and Article JSON-LD. | Feed returned 200 with `Last-Modified`; pages returned 200. [`robots.txt`](https://arstechnica.com/robots.txt) permits ordinary article paths for `*`, but disallows several named AI bots. |
| [SVT Nyheter](https://www.svt.se/rss.xml) | The configured historical URL `https://www.svt.se/nyheter/rss.xml` returned a 302 to the URL at left. The final RSS returned **100** items, each with `description` but no `content:encoded`, Media RSS, or enclosure. The first description was 201 characters. | Three sampled linked articles returned 200. The first has a self-canonical, [this article](https://www.svt.se/nyheter/inrikes/i-fel-hander-kan-ai-vara-ett-mycket-farligt-vapen), and HTML article/text body (the sample included `TextArticle__body`). | The page provides description, canonical, `og:*` image dimensions/alt, and `NewsArticle` JSON-LD with author, dates, image, and (for the sample) `VideoObject`. | Feed/page probes returned 200 after the feed redirect. [`robots.txt`](https://www.svt.se/robots.txt) allows `*` and expresses `ai-input=yes, ai-train=no`; it also names retrieval bots as allowed and training bots as disallowed. |
| [Sveriges Radio Ekot](https://api.sr.se/api/rss/program/83) | Atom returned **20** entries. Each had HTML `content`; the first had 874 characters versus a 330-character `summary`, including an inline image, image caption, byline, and a “Lyssna” audio link. The first three `content` values were 874/1,070/1,024 characters. | The first entry’s HTTPS article URL, [article 9272244](https://www.sverigesradio.se/artikel/9272244), returned **403 Access Denied** in this probe. Thus this research cannot validate a page selector or HTML canonical for Ekot. | The Atom content itself supplies title, published/updated times, author, inline image URL/caption, and audio link. It has no enclosure. | Feed returned 200 (`Cache-Control: public,max-age=60`). Page fetch and `https://www.sverigesradio.se/robots.txt` both returned 403 from the edge; do not treat an unavailable robots document as permission, and do not work around the block. |

The regular Ars feed is therefore explicitly a preview route. David is the only observed feed that directly demonstrated full HTML by comparison with its linked page. SVT is a metadata/summary discovery feed. Ekot supplies richer feed HTML, but, because its page was blocked, its semantic completeness is **unproven** rather than “proved full text.” Short radio news may genuinely be short; length alone cannot establish completeness.

All linked-page conclusions are limited to the samples above. Paywalls, changed markup, geo/edge restrictions, video-only items, and publisher policy changes remain possible.

## Recommended deterministic acquisition contract

### 1. Discovery, identity, and canonicalization

1. Configure exactly these discovery URLs: David’s RSS URL above, Ars’ index feed above, SVT’s final `https://www.svt.se/rss.xml`, and Ekot’s Atom URL above. Persist the final feed URL after an allowed same-origin redirect.
2. Store the raw entry and the normalized fields: source key, original link, title, published/updated instant, author, summary, body candidate, and all image/audio candidates. Resolve relative URLs against the response URL.
3. Canonical Article key: use the final page’s `<link rel="canonical">` only after a successful same-origin fetch; otherwise use the feed link. Normalize scheme/host case, remove a fragment, and remove only known tracking keys (`utm_*`, `gclid`, `fbclid`). Preserve every other query parameter. Never adopt a cross-origin canonical or redirect without an explicit source rule.
4. Deduplicate on `(source key, canonical Article key)`; retain the original feed URL for attribution and diagnostics.

### 2. Fixed source routes

| Source | Primary body route | Page fallback | Deterministic result when the route is not complete |
| --- | --- | --- | --- |
| David | Sanitized `content:encoded`. | Fetch the feed link and extract the semantic `article` only if the feed body is invalid/empty. | `BODY_UNAVAILABLE`; keep discovery metadata and original URL. |
| Ars | Fetch the feed link; extract the semantic article body. Do **not** use regular `content:encoded` as the body because it has the “Read full article” signpost. | None beyond one allowed page fetch. Subscriber feeds are not silently substituted. | `BODY_UNAVAILABLE`; do not publish the preview as a full Article. |
| SVT | Fetch the feed link; extract semantic article text, using JSON-LD only for metadata/image/video fallback. | None beyond one allowed page fetch. | `BODY_UNAVAILABLE`; do not expand the RSS description into a body. |
| Ekot | Sanitized Atom `content[type=html]`, including its image/caption and audio URL as source-provided metadata. | One normal fetch of the entry link, only when access is allowed. | `DEGRADED_SOURCE_BODY` if Atom content is substantive; mark completeness **unverified** and add a Publication Note. Otherwise `BODY_UNAVAILABLE`. A 401/403/451 must not trigger a bypass or retry. |

Sanitize HTML before EPUB generation: preserve readable paragraphs, headings, lists, block quotes, tables, figures/captions, emphasis, and safe links; remove scripts, styles, forms, embeds, tracking pixels, navigation, and advertising. Do not use a search engine, sitemap, AMP guess, or a third-party mirror as a fallback: those routes are neither source-equivalent nor deterministic.

### 3. Completeness decision

Apply these rules after whitespace normalization and boilerplate removal. The thresholds are deliberately policy constants, stored with the Run ID.

1. `FULL_SOURCE_BODY`: a configured full-body route produces at least 80 words, has no case-insensitive signpost `read full article`, and has at least one article-structure element (`p`, `li`, heading, quote, or table). David is the observed positive case.
2. `FULL_PAGE_BODY`: an allowed page extract produces at least 80 words and a title plus either a canonical URL or a published date. Ars and SVT use this route. A short item (20–79 words) may be accepted only when the page itself supplies that identity evidence; record `SHORT_AS_PUBLISHED` rather than inflating it with the feed summary.
3. `SUMMARY_ONLY`: fewer than 20 words, a known signpost, an empty extraction, or a selector/identity mismatch. It is eligible only for a linked “Bonus Reads” index, never a purported complete Article body.
4. `DEGRADED_SOURCE_BODY`: the Ekot feed body passes the structure/80-word test but the linked page cannot be validated. Preserve it exactly (after safety sanitization), set `completeness=unverified`, and emit a concise Publication Note with the Run ID. It must not be labelled `FULL_*`.

For an accepted body, metadata precedence is: feed title/date/author when present; page JSON-LD or Open Graph only to fill missing values; never replace a non-empty publisher feed value merely because it differs. Lead-image precedence is source-body `<img>` or Media RSS image, then page `NewsArticle.image`, then `og:image`; reject non-HTTP(S), cross-origin URLs unless configured, and known site-default/logo images. A failed image download removes only the image, not a valid body.

### 4. Polite failure, retry, and cache behavior

* Send a stable, descriptive User-Agent with a project contact URL; obey the matching `robots.txt` group and applicable publisher terms before scheduled operation. Treat access policy changes as configuration failures, not parser failures.
* Conditional-fetch stored ETags/`Last-Modified` values. The observed David feed exposes an ETag and Ars exposes `Last-Modified`; a 304 reuses the stored raw response. SVT’s observed feed TTL was 88 seconds and Ekot’s was 60 seconds, so do not poll either more frequently than its advertised freshness.
* Retry only transport failures, 408, 429, and 5xx: at +2 seconds then +10 seconds, respecting `Retry-After` when present (cap wait at 60 minutes and schedule rather than block a run). Never retry 401/403/404/410/451 in that Edition, and never rotate identity, proxies, or headers to evade a block.
* On transient failure, an explicitly configured cached, previously accepted body may be used only if it is at most 24 hours old, with `stale=true` and a Publication Note. Otherwise emit `BODY_UNAVAILABLE` and retain the attribution/link. No failure may silently turn a summary into a full body.

## Reproducibility

From a networked shell, use a rate-limited, read-only probe such as:

```sh
curl -sS -L --compressed --max-time 30 \
  -A 'epub-news-feeder research contact: https://github.com/davelowelarsson/epub-news-feeder' \
  -D headers.txt -o feed.xml https://www.svt.se/rss.xml
xmllint --xpath "count(//*[local-name()='item'])" feed.xml
```

Then fetch no more than three current entry links per source sequentially, record HTTP status/final URL/content type, and compare the normalized body with the rules above. Re-run this research before enabling a new source, altering a selector, or treating Ekot content as verified full text.
