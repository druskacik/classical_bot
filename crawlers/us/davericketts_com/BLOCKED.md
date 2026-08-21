<!-- crawler-factory-metadata
{"url":"https://davericketts.com/","geographic_scope":"country","country_code":"US","reason_code":"no_current_events","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# Crawler blocked: no current in-scope events

The supplied URL is https://davericketts.com/. It is the website of a San
Francisco-based US guitarist, vocalist, and composer, so the resolved geography
is the United States (`US`).

The site's home page includes a Squarespace "Gigs & Shows" calendar. The
calendar currently has no events, and its most recent archived listing was in
April 2024. A working crawler cannot currently provide any in-scope concert
records.

## Investigation

- Playwright network inspection found the structured Squarespace endpoint
  `/api/open/GetItemsByMonth`, using collection ID
  `5b886fae8a922d94208dac8d`. The required `crumb` is issued as a cookie when the
  home page is loaded, so the API itself is reconstructible.
- Monthly API requests were tested for every month from January 2017 through
  December 2026. The month and collection identifiers persisted across the full
  date range; the endpoint is month-based and exposes no separate pagination.
  It returned 224 archived records dated from September 2018 through April
  2024, and no later records.
- The feed exposes no event-type, discipline, series, or genre query filter.
  Records have optional `categories` and `tags`, but the API still returns the
  unfiltered month. Observed category values were `Country Guitar Music`,
  `Blues Guitar Music`, `Jazz Guitar Music`, and `Classical Guitar Music`;
  observed tags were `Livestream`, `Gaucho Jazz`, `live jazz`, `live music`, and
  `sunday brunch`.
- The only apparently classical subset was 18 records titled "Livestream
  Classical Guitar Music". Representative API records explicitly describe a
  performance streamed from the artist's Facebook page and carry the
  `Livestream` tag. The project inclusion guidance excludes streaming-only
  events. The rest of the archive consists predominantly of jazz, country and
  blues livestreams, bar engagements, and similar nonclassical gigs.
- Representative archived detail URLs now return HTTP 404. The API still
  exposes their title, timestamps, location, excerpt, category, and tags, but
  those fields do not reveal any other in-scope performances.

## What would unblock implementation

Implementation can be retried if the first-party calendar publishes new
concrete, in-person classical performances (or restores qualifying archived
detail pages). The monthly Squarespace API can then be used as the primary
structured source. Because this artist calendar is a mixed feed and has no
stable server-side genre filter, a future crawler should use
`upload_target="potential"` unless a comprehensive, reliably filtered
first-party feed is introduced.
