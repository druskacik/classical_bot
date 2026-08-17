<!-- crawler-factory-metadata
{"url":"https://sebagomusicfestival.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Access blocked

## Original URL

https://sebagomusicfestival.org/

## Why a crawler cannot currently be implemented

The site is a US-based, classical-only chamber music source with published 2026 concerts, but its StackProtect security layer returns HTTP 401 Security Verification pages to automated clients. The protection applies to both listing/detail pages and normal discovery endpoints, so there is no first-party response that a production crawler can currently parse.

## Approaches attempted

- Opened the canonical homepage with Playwright and inspected its network traffic. The document returned HTTP 401; the only dynamic request was a StackProtect challenge endpoint, which returned HTTP 403. No concert API request was exposed.
- Tested likely first-party HTML routes, including `/concert-tickets-2026/`, `/events/`, `/concerts/`, and `/schedule/`. They returned the same verification document rather than concert HTML.
- Tested WordPress API forms `/wp-json/`, `/wp-json/wp/v2/pages`, `/wp-json/wp/v2/posts`, and `?rest_route=/wp/v2/pages`. All were intercepted with HTTP 401.
- Tested discovery routes `/robots.txt`, `/sitemap.xml`, and `/wp-sitemap.xml`, plus `www` and HTTP host variants and a crawler user agent. These did not bypass the protection.
- Verified through indexed public results that the first-party site has a concrete `Concert Tickets 2026` page with five dated Deertrees Theatre performances and a `Community Concerts` page with additional dated performances. Search-engine extracts are not a stable first-party feed and therefore were not used as a crawler input.

The site exposes no usable genre, category, discipline, event-type, series, or tag filter through the accessible verification response. Pagination and date-range persistence therefore could not be tested. The intended source is already classical-only, so a working first-party feed would qualify for the `classical` upload target.

## What would unblock implementation

Allowlisting the production crawler, removing the StackProtect challenge from public concert and WordPress API routes, or exposing a stable first-party JSON, RSS, iCalendar, or HTML feed that can be fetched without an interactive security challenge would unblock implementation.
