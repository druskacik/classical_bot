<!-- crawler-factory-metadata
{"url":"https://www.tsoonline.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# Crawler blocked by site-wide robot challenge

## Original URL

https://www.tsoonline.org/

The source is the Tuscaloosa Symphony Orchestra in Tuscaloosa, Alabama, so the resolved country code is `US`.

## Why a crawler cannot currently be implemented

Every tested first-party route is intercepted by SiteGround's robot protection before the application serves any concert data. Responses have HTTP status 202, the `sg-captcha: challenge` header, and a redirect to `/.well-known/sgcaptcha/` or `/.well-known/captcha/`. The challenge requires an image CAPTCHA and cookies. A production `requests` crawler cannot reliably or appropriately solve that interactive challenge.

Search-engine results show that the orchestra recently published concrete concert and ticket-package content, but cached search snippets are neither complete nor a stable first-party source and therefore cannot support a universal production crawler.

## Approaches attempted

- Opened the canonical homepage with Playwright and inspected its network requests. The browser was redirected to a robot challenge; network traffic exposed only challenge HTML and static CAPTCHA assets, with no concert API request.
- Inspected the rendered challenge with Playwright. It contains an image CAPTCHA, text input, and Continue button; no underlying event content is present.
- Requested the homepage and plausible HTML listing routes (`/events/` and `/concerts-events/`) directly with a normal browser user agent. All returned the same challenge response.
- Requested `/robots.txt` and `/sitemap.xml` to discover first-party routes. Both were challenge-protected rather than returning crawl directives or URL indexes.
- Tested the likely WordPress REST API at `/wp-json/wp/v2/pages?per_page=100`, including with a crawler user agent. It was also challenge-protected and returned no JSON.
- Searched indexed results to verify the organization and whether concert content exists. Results identified the Tuscaloosa Symphony Orchestra and a recently indexed 2025-2026 ticket-package page, but did not expose a complete, pageable, first-party event feed or stable category/filter identifiers.

Because no event feed could be reached, no first-party genre, category, discipline, event-type, series, or tag filters could be tested for exact values or pagination persistence. Source purity and adjacent-filter coverage could likewise not be validated, so no upload target can be selected responsibly.

## What would unblock implementation

Any stable first-party endpoint that is accessible without an interactive CAPTCHA would unblock the crawler, for example:

- allowlisting the production crawler's network identity;
- disabling the challenge for public read-only sitemap, event-listing, and event-detail routes;
- exposing an accessible WordPress REST API or calendar JSON/ICS feed; or
- providing a documented ticketing/event API that contains dates, titles, venues, cities, detail URLs, and programme descriptions.

Once access is available, the event feed and all relevant category filters must be inspected across pagination and date ranges before choosing `classical` versus `potential` upload.
