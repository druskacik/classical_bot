<!-- crawler-factory-metadata
{"url":"https://hksl.org/","geographic_scope":"country","country_code":"HK","reason_code":"access_blocked","attempted_at":"2026-08-14","retry_after":"2026-09-13"}
-->

# Hong Kong Sinfonietta crawler blocked

## Original URL

https://hksl.org/

The source is the Hong Kong Sinfonietta, a Hong Kong-based orchestra. Its
resolved geographic scope is therefore Hong Kong (`HK`), even though its
concert listing also contains touring performances outside Hong Kong.

## Why a crawler cannot currently be implemented

Cloudflare returns HTTP 403 challenge pages for the concert catalogue, concert
detail pages, the XML sitemap, and WordPress REST API routes. The challenge did
not clear in a JavaScript-enabled Playwright browser, and ordinary Python HTTP
requests received the same response. A production crawler using the
repository's HTTP-based crawler pattern would therefore be unable to retrieve
or parse records reliably.

The public `robots.txt` file is reachable and advertises
`https://hksl.org/sitemap_index.xml`, but the advertised sitemap itself is
challenge-blocked. Search-engine results show that `/concert/` currently
contains concrete performances and historical concert detail URLs, so this is
not an empty or unrelated source.

## Approaches attempted

- Loaded `https://hksl.org/`, `https://hksl.org/concert/`, and the `www`
  variant with Playwright and inspected their network traffic. Only the
  Cloudflare challenge flow was exposed; no concert API request was made.
- Waited for the browser challenge to complete, but the page remained an HTTP
  403 "Just a moment" response.
- Tested likely WordPress API discovery and collection routes:
  `https://hksl.org/wp-json/`, `https://hksl.org/wp-json/wp/v2/types`, and
  `https://hksl.org/wp-json/wp/v2/concert?per_page=1`. All returned the same
  Cloudflare challenge rather than JSON.
- Tested HTML access to the English concert catalogue over HTTPS and HTTP and
  with a browser user agent. All variants returned HTTP 403.
- Retrieved `https://hksl.org/robots.txt`, then tested its advertised
  `sitemap_index.xml`; the sitemap returned HTTP 403.
- Reviewed indexed catalogue and representative detail-page results. They show
  dates, times, venues, multi-date performances, programme text, Hong Kong
  events, and Portugal tour events, but search results are neither a complete
  first-party feed nor a stable crawler interface.

## Filters and upload-target assessment

No applicable first-party genre, category, series, tag, or event-type filter
could be inspected because both the HTML catalogue and API discovery routes
were blocked. Consequently, no filter values could be verified across
pagination or date ranges. Indexed evidence suggests the catalogue is
primarily classical and includes eligible orchestral, chamber, family, and
crossover programmes, but it also includes at least one tour listing described
as a solo jazz-improvisation event. If access is restored and that contamination
cannot be excluded with stable, comprehensive first-party filters, the crawler
should use `upload_target="potential"`; otherwise a verified classical-only
feed may use `classical`.

## What would unblock implementation

Any of the following would permit a reliable implementation and validation:

- Cloudflare allowing the crawler runtime to access catalogue, detail, sitemap,
  and API routes without an interactive challenge;
- a documented or allow-listed first-party JSON, RSS, iCalendar, or XML feed;
- a stable origin/API hostname supplied by the organization; or
- a reproducible, authorized non-interactive access method suitable for the
  production crawler runtime.

After access is restored, the catalogue and archive pagination, every relevant
first-party filter, representative adjacent categories, multi-date expansion,
tour geography, and detail-page programme extraction must be tested before an
upload target is selected.
