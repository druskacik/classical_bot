<!-- crawler-factory-metadata
{"url":"https://ofit.com.mx/","geographic_scope":"country","country_code":"MX","reason_code":"access_blocked","attempted_at":"2026-08-14","retry_after":"2026-09-13"}
-->

# Crawler blocked

## Original URL

https://ofit.com.mx/

The domain belongs to the Orquesta Filarmónica de Toluca, a Mexico-based classical orchestra. The resolved geography is therefore Mexico (`MX`).

## Why implementation is currently blocked

The source cannot currently be accessed reliably from the crawler environment. Browser navigation to the canonical HTTPS homepage enters a redirect loop (`ERR_TOO_MANY_REDIRECTS`). Subsequent requests are rejected by Cloudflare with HTTP 429 / Error 1015, stating that the client has been temporarily rate limited. The same behavior affects both apex and `www` host variants and prevents discovery of a concert catalogue or archive.

Without a reachable first-party listing, API, or detail page, there is no defensible way to enumerate all published performances or extract required dates, venues, and cities. Search-engine results confirm the organization identity but do not expose a stable first-party concert feed and cannot substitute for a universal first-party crawler.

## Approaches attempted

- Tested `https://ofit.com.mx/`, `http://ofit.com.mx/`, `https://www.ofit.com.mx/`, and `http://www.ofit.com.mx/` with Playwright; all host/protocol variants entered redirect loops.
- Inspected browser network traffic before attempting HTML parsing. The only document request failed in the redirect loop, so no XHR, Fetch, GraphQL, or other structured event API could be reconstructed.
- Probed common first-party discovery endpoints and paths: `/robots.txt`, `/sitemap.xml`, `/wp-json/`, and `/eventos/`. These were either caught in the redirect loop or returned Cloudflare HTTP 429 / Error 1015.
- Checked indexed results for pages on `ofit.com.mx` and for OFiT concert/calendar terms. No scrapeable first-party event listing or archive was exposed.
- No genre, category, discipline, event-type, series, or tag filters could be tested because the site and its discovery endpoints were inaccessible. Consequently, persistence across pagination and date ranges could not be evaluated.

## What would unblock implementation

Implementation can resume when the redirect configuration is repaired and Cloudflare permits normal read-only access from the production crawler environment. Alternatively, the organization could provide a stable public event API, calendar feed, sitemap, or accessible archive containing concrete performances and their detail pages.

Once access is restored, investigation should begin again with browser network requests, verify any first-party filters through pagination and date ranges, inspect representative current and archived detail pages, and then choose the upload target based on the actual feed coverage.
