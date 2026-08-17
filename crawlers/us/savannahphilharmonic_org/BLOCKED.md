<!-- crawler-factory-metadata
{"url":"https://savannahphilharmonic.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Savannah Philharmonic crawler blocked

## Original URL

https://savannahphilharmonic.org/

The source is the Savannah Philharmonic Orchestra and Chorus in Savannah,
Georgia, so the resolved country is the United States (`US`). The site still
publishes concrete concerts, including its 2026–2027 season, but all tested
first-party pages and machine-readable endpoints are currently behind a
Cloudflare managed challenge that returns HTTP 403 to the crawler environment.

## Investigation performed

- Opened the home page with Playwright and inspected its network traffic. The
  browser received only the Cloudflare "Performing security verification"
  page and a Turnstile request; no application event API request was exposed.
- Tested the event archive at `/events/` and `/events/list/`. Indexed copies
  identify it as a WordPress The Events Calendar view and currently show zero
  events, while direct access is blocked by the same challenge.
- Tested the WordPress REST index (`/wp-json/`), a page API request
  (`/wp-json/wp/v2/pages?slug=2026-2027-season`), and the likely The Events
  Calendar API (`/wp-json/tribe/events/v1/events`). Every endpoint returned the
  Cloudflare HTML challenge instead of JSON.
- Tested `/wp-sitemap.xml`, the static `/2026-2027-season/` page, and individual
  event-style paths through both Playwright and a normal HTTP session with a
  browser user agent. These also returned HTTP 403 challenge pages.
- Search-engine indexing was used only to confirm that the source is not empty.
  It exposes orchestral season performances and separate community concert
  occurrences, but cached search snippets are neither a stable first-party feed
  nor suitable production input.

No usable genre, category, discipline, event-type, series, or tag filter could
be inspected because the application never loaded. The indexed `/events/`
calendar exposes keyword and date navigation but no applicable first-party
artistic filter. Pagination/filter persistence therefore could not be verified.

## What would unblock implementation

Implementation can proceed when the site permits non-interactive access to at
least one stable first-party source, preferably the WordPress/The Events
Calendar REST API, or when the site operator provides an allowlisted API/feed.
Access to ordinary season, community-concert, archive, and detail-page HTML
would also be sufficient for an HTML crawler. At that point the event calendar,
season page, and adjacent community-performance pages should be compared for
coverage before selecting a feed and upload target.
