<!-- crawler-factory-metadata
{"url":"https://youngstownsymphony.com/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# Access blocked

Original URL: https://youngstownsymphony.com/

The Youngstown Symphony Orchestra site currently returns Cloudflare HTTP 522
(`Connection timed out`) for every live request. The source is the US-based
Youngstown Symphony Orchestra in Youngstown, Ohio, and is not a multi-country
calendar.

## Investigation performed

- Opened both the canonical homepage and the `www` hostname with Playwright.
  Both returned HTTP 522 before the application loaded.
- Inspected Playwright's network log. It contained only the failed document
  request and Cloudflare error-page assets, so there were no application API or
  XHR requests to reconstruct.
- Requested the likely WordPress REST API at `/wp-json/wp/v2/search` with a
  known concert title. It also returned HTTP 522.
- Requested the current season page (`/2026-2027-season/`) and archive page
  (`/past-events/`) through the live origin. They returned the same HTTP 522.
- Verified through recently indexed search results that the site is not empty:
  the current season page lists dated 2026/2027 concerts, the past-events page
  contains an archive back to at least 2022, and concert detail pages under
  `/blog/` contain dates, venues, descriptions, and repertoire. Indexed copies
  are not a stable first-party endpoint suitable for a production crawler.

## Filters and feed assessment

No live first-party genre, category, discipline, event-type, series, or tag
filter could be inspected because the origin never served the application.
The visible first-party navigation exposes a current `2026/2027 Season` page
and a `Past Events` archive rather than a paginated category filter. Indexed
archive results include orchestral concerts and crossover performances, but
also entries such as `A NIGHT FOR JAZZ` and `RENT`; therefore the archive cannot
be proven uniformly in scope. If access is restored and those same feeds remain
mixed or their relationship to the orchestra is ambiguous, the crawler should
use `upload_target="potential"` rather than uploading the unfiltered archive as
classical.

## What would unblock implementation

Restore the site's origin behind Cloudflare (or provide a stable first-party
API/feed reachable without the timed-out origin). Once live access is available,
the WordPress REST routes and page-builder network requests can be inspected,
pagination and archive coverage can be verified, representative adjacent event
types can be checked against the inclusion guidance, and a parser can be tested
against real current and historical detail pages.
