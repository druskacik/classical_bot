<!-- crawler-factory-metadata
{"url":"https://www.billyjoel.com/","geographic_scope":"country","country_code":"US","reason_code":"no_current_events","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# No in-scope concerts

The original URL is https://www.billyjoel.com/. It is the official website of
US pop/rock artist Billy Joel. The source currently publishes no upcoming tour
dates, and its available Tour History consists of Billy Joel pop/rock concerts,
which are outside the project's classical-music scope.

## Investigation performed

- Loaded the home page, `/tour/`, `/tour-history/`, and representative event
  detail pages with Playwright.
- Inspected browser network requests before considering HTML parsing. Neither
  the current Tour page nor Tour History called a concert API, WordPress REST
  endpoint, GraphQL endpoint, or event-provider feed; concert data is rendered
  in the first-party HTML response.
- Inspected the current Tour HTML. Its tour container is empty and has no
  pagination, dates, or event records.
- Inspected Tour History's HTML, year links, pagination, and representative
  records including Billy Joel solo and Billy Joel & Sting stadium/arena shows.
  The archive has parseable dates, cities, venues, and detail URLs, but those
  records are pop/rock performances rather than in-scope classical events.
- Looked for first-party genre, category, discipline, event-type, series, and
  tag filters. The artist-specific source exposes none. The only archive filter
  is `tour_year` (for example `tour_year=2025` and `tour_year=2024`), plus text
  search; neither is an artistic-scope filter.

## What would unblock implementation

A retry is appropriate if the official site begins publishing a concrete
classical, orchestral, classical-crossover, or otherwise in-scope performance
with a valid date, city, and venue. At that point the first-party HTML can be
parsed directly; if a new tour frontend is introduced, its network requests
should be checked again for a structured event feed.
