<!-- crawler-factory-metadata
{"url":"https://traversesymphony.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Access blocked

## Original URL

https://traversesymphony.org/

The Traverse Symphony Orchestra has been renamed Traverse City Philharmonic, and its current first-party website is `https://tcphil.org/`. The organization remains based in Michigan, United States, so the resolved country code is `US`.

## Why a crawler cannot currently be implemented

Both the supplied domain and the current first-party domain are protected by SiteGround's robot challenge. A normal unattended HTTP client receives status `202` with a short HTML meta-refresh to `/.well-known/sgcaptcha/` instead of the requested page or JSON. This also affects the otherwise suitable events API, so a production `requests`-based crawler cannot currently retrieve records reliably.

The Playwright browser could access the API only after SiteGround issued a browser-specific `_I_` clearance cookie. That cookie is challenge-generated, expires, and must not be copied into production code. The repository has no supported browser runtime in the crawler interface, and attempting to automate a CAPTCHA would be inappropriate and fragile.

## Approaches attempted

- Opened the original home page and common listing paths (`/events/`, `/concerts/`, and `/event/`) with Playwright. They initially returned the SiteGround robot challenge.
- Probed WordPress discovery endpoints (`/wp-json/`, `/?rest_route=/`, `/wp-sitemap.xml`, and `/robots.txt`). The original domain returned the same challenge for every endpoint.
- Followed first-party rebranding evidence to `https://tcphil.org/concerts/` and inspected its browser network behavior.
- Reconstructed The Events Calendar REST API at `https://tcphil.org/wp-json/tribe/events/v1/events` and verified in the cleared Playwright context that it returns structured titles, occurrence dates and times, canonical event URLs, venue objects, descriptions, organizers, categories, and tags.
- Tested the default future feed with `per_page=10`, page 2 with `page=2`, and the archive feed with `start_date=2000-01-01 00:00:00` on pages 1 and 2. Pagination parameters persisted in `next_rest_url`; the archive reported 68 occurrences across 7 pages, while the default future feed reported 15 across 2 pages.
- Inspected first-party category values including `traverse-city-philharmonic` (ID 62), `tc-phil-professional` (ID 70), `masterworks` (ID 78), `pops` (ID 79), `series-at-the-center` (ID 80), `traverse-city-jazz` (ID 61), `free-community-event` (ID 63), and `civic-string-orchestras` (ID 73). The overall calendar is mixed: representative records included orchestral masterworks and civic strings, but also jazz, a comedy cabaret, and a non-performance networking event. Some uncertain events share IDs 62 and 70 with clearly classical concerts, so those categories are not clean enough for direct classical upload. A future implementation should therefore ingest the complete concrete-event API feed with `upload_target="potential"` rather than rely on a narrow category.
- Repeated the API request using system Python and a browser-like user agent. It received status `202` and CAPTCHA HTML instead of JSON, confirming that the API is inaccessible to the production HTTP client even though it works in a challenge-cleared browser session.

## What would unblock implementation

Any stable, unattended first-party access path would unblock the crawler: removal or relaxation of the SiteGround challenge for the public REST API, an allowlist for the crawler's production egress, or a documented API/feed hostname that does not require challenge cookies. Once available, the The Events Calendar REST endpoint can be paginated from an early `start_date` to include both archived and future occurrences and sent to the potential-event classifier.
