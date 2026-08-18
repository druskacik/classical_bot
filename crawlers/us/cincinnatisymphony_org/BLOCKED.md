<!-- crawler-factory-metadata
{"url":"https://www.cincinnatisymphony.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-18","retry_after":"2026-09-17"}
-->

# Cincinnati Symphony Orchestra crawler blocked

## Original URL

https://www.cincinnatisymphony.org/

## Why a crawler cannot currently be implemented

The public website redirects every tested request through the Cincinnati Symphony Orchestra's secure shared-session endpoint. That endpoint is protected by Imperva and hCaptcha and returns challenge markup instead of the requested page. The same protection applies to the secure event calendar. A normal production HTTP client therefore cannot retrieve either the listing HTML or structured event data reliably.

The organization and its events are based in the United States, so the resolved country code is `US` and the geographic scope is a single country.

## Approaches attempted

- Opened the homepage and `/sitemap.xml` with Playwright. Both redirected to `https://secure.cincinnatisymphony.org/components/sharedsession`, with the intended public URL supplied as `returnUrl`.
- Inspected Playwright network traffic before and after waiting for the shared-session redirect. No concert API response was made available; traffic consisted of the Imperva challenge and hCaptcha configuration requests.
- Opened `https://secure.cincinnatisymphony.org/events` directly with Playwright. It produced the same challenge and no parseable calendar DOM.
- Requested the public site with a normal HTTP session, both with and without redirects. The initial response was a shared-session redirect and the destination returned an Imperva "Request unsuccessful" page rather than site HTML.
- Investigated indexed event URLs and the secure calendar query surface. Search indexing shows concrete event detail pages and a calendar at `/events`, including an observed keyword parameter such as `k=HarmonyPass`, but a keyword search is not a reliable first-party category filter and the protected live pages could not be inspected for stable genre, series, pagination, or date-range identifiers.

Because neither listing/detail HTML nor a structured API can be accessed, representative records cannot be validated for dates, venues, cities, descriptions, pagination, historical coverage, or category contamination. Creating a scraper against challenge markup or search-engine snippets would not be a working universal crawler.

## What would unblock implementation

Any one of the following would allow another implementation attempt:

- allowlisting the crawler's production egress address with the site's Imperva configuration;
- a first-party public event API or export that is accessible without a browser challenge;
- removal of the challenge from read-only public listing and detail pages; or
- authenticated access credentials and documented permission for a stable machine-readable calendar endpoint.

