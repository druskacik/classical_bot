<!-- crawler-factory-metadata
{"url":"https://www.kennedy-center.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-18","retry_after":"2026-09-17"}
-->

# Kennedy Center crawler blocked

## Original URL

https://www.kennedy-center.org/

The source is the US-based John F. Kennedy Center for the Performing Arts in Washington, DC, so the resolved country code is `US`.

## Why a crawler cannot currently be implemented

Cloudflare returns an HTTP 403 challenge page before the Kennedy Center application loads. The challenge did not clear in the Playwright browser, and ordinary HTTP requests receive the same response. Consequently, no event listing HTML, detail-page HTML, structured event data, pagination controls, or application network requests are available to a production crawler from this environment.

The site is a mixed performing-arts source. Search-engine evidence shows first-party paths for `classical-music`, `opera`, `ballet`, `dance`, and series such as `fortas`, but those paths cannot currently be inspected deeply enough to verify taxonomy coverage, contamination, concrete-occurrence behavior, pagination identifiers, or archive/date-range behavior. A crawler must not treat the narrow `classical-music` path as comprehensive without those checks, and an unfiltered mixed feed must not upload directly as classical.

## Approaches attempted

- Opened `https://www.kennedy-center.org/whats-on/` with Playwright and inspected its network traffic. The document returned 403 and exposed only Cloudflare challenge requests, not an event API.
- Waited for the browser challenge to resolve; it remained on the 403 `Just a moment...` page.
- Requested first-party genre paths with exact URL values `classical-music`, `opera`, `ballet`, and adjacent `dance`. All returned the Cloudflare challenge, so their contents and pagination persistence could not be tested.
- Requested a representative indexed classical detail URL (`/whats-on/explore-by-genre/classical-music/2025-2026/berlin-in-lights-postclassical/`); application content was not accessible.
- Probed likely JSON routes including `/api/events`, `/api/search`, `/api/whatson`, and `/api/whats-on`; these were also intercepted by Cloudflare and no usable API was found.
- Requested `/sitemap.xml`; it was blocked. `/robots.txt` is accessible but contains crawl directives only and no event records or sitemap location.
- Checked indexed current and historical event URLs. They confirm that concrete concerts have been published, including classical-music and Fortas pages, but search results are not a complete, stable, or first-party scrapeable feed.

## What would unblock implementation

Any stable first-party access path that is available to the production runtime would unblock the crawler, for example:

- allowlisting the crawler/runtime at Cloudflare;
- a documented or discoverable public event JSON endpoint and its pagination/filter contract;
- a complete accessible sitemap or server-rendered listing/detail pages; or
- authenticated access credentials explicitly intended for automated event retrieval.

Once access is restored, investigation must compare the exact `classical-music`, `opera`, `ballet`, `dance`, Fortas, National Symphony Orchestra, and Washington National Opera filters (plus adjacent music/family filters), verify their stable identifiers across pages and past/future date ranges, and then choose `classical` only if their combined feed is comprehensive and uncontaminated. Otherwise the appropriate target is `potential` for the mixed candidate feed.
