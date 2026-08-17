<!-- crawler-factory-metadata
{"url":"https://symphonytacoma.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Symphony Tacoma crawler blocked

Original URL: https://symphonytacoma.org/

Symphony Tacoma is a US organization whose published concerts are based in Tacoma, Washington. Search-indexed first-party pages confirm that the site has concrete concert listings and archives, but the live source currently blocks automated access with a SiteGround robot challenge. Every tested route returns HTTP 202 and a redirect to `/.well-known/sgcaptcha/` instead of event data, so a production crawler would not have a dependable input.

## Approaches attempted

- Opened the homepage with Playwright and inspected its network requests first. Navigation was replaced by a "Robot Challenge Screen" before any concert API or calendar request occurred; the only subsequent requests were challenge assets from SiteGround's CloudFront host.
- Tested WordPress REST discovery at `/wp-json/` and `/wp-json/wp/v2/types`, plus a likely event collection at `/wp-json/wp/v2/event?per_page=5`. All returned the same challenge HTML rather than JSON.
- Tested normal HTML routes including `/concerts/`, `/concerts/upcoming-concerts/`, `/events/`, and `/concerts-events/`, as well as `/robots.txt`. These were also intercepted by the challenge.
- Repeated endpoint checks over HTTP and HTTPS and with both the apex and `www` hostnames. The challenge persisted in every case.
- Reviewed search-indexed first-party listing and detail pages to verify that events exist and are parseable in principle. The indexed upcoming-concert page contains concrete Symphony Tacoma performances, and indexed detail pages expose title, date, time, venue/programme text. However, search-engine excerpts are neither a complete nor stable first-party feed and cannot support reliable pagination or archive coverage.

## Filters and upload-target assessment

No usable first-party genre, category, discipline, series, tag, event-type, pagination, or date-range filter could be inspected because the challenge intercepts both HTML and REST routes before the application responds. The indexed material indicates this is a classical-only orchestra source whose orchestra, choral, pops, family, and education performances fit the project's inclusion guidance; if access is restored, the expected upload target is `classical`. That assessment still needs live verification against the complete listing and adjacent site sections.

## What would unblock implementation

Any stable server-readable first-party source would unblock the crawler: allowlisted access to the site's listing/detail HTML or WordPress REST API, a public calendar JSON/ICS feed, or documented API endpoints that bypass the challenge. Once available, the event feed must be checked across pagination and past/future date ranges, and representative orchestra, choral, pops, family, education, and adjacent non-performance pages must be inspected before implementation.
