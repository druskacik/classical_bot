<!-- crawler-factory-metadata
{"url":"https://www.npac-weiwuying.org/","geographic_scope":"country","country_code":"TW","reason_code":"access_blocked","attempted_at":"2026-08-15","retry_after":"2026-09-14"}
-->

# Crawler blocked

## Original URL

https://www.npac-weiwuying.org/

## Why a crawler cannot currently be implemented

The National Kaohsiung Center for the Arts (Weiwuying) is a Taiwan-based,
mixed-discipline venue with concrete current and archived program pages, but its
AWS load balancer returns `403 Forbidden` to every request from the crawler
environment. The response is the same short load-balancer error page and does
not contain the site's application HTML or event data. A production crawler
built against this environment would therefore return no records or fail on
every run.

Search-indexed first-party pages show music, theatre, dance, circus, talks, and
other programs, so an unfiltered feed could not safely upload directly to the
classical table. They also show a `catalog` query parameter, but the inaccessible
site prevents verification of the complete category taxonomy, stable category
IDs, pagination, date-range behavior, or adjacent-category coverage. Cached
search results are incomplete and are not a stable or pageable crawler source.

## Approaches attempted

- Opened the supplied homepage and the canonical host without `www` in
  Playwright. Both returned HTTP 403 before loading the application.
- Inspected Playwright network requests. Only the blocked document request was
  present; no XHR, fetch, GraphQL, or other API request was emitted, so there was
  no API request to reconstruct.
- Tested the first-party filtered listing
  `/programs?catalog=5aec418cb01ea6000520f635&lang=en`, which is indexed with
  program category labels. It returned the same 403, so the filter and its
  pagination could not be validated.
- Tested the first-party monthly calendar
  `/calendar?catalog=&lang=en&t=202608&type=program`. It returned the same 403,
  preventing date-range and pagination checks.
- Tested a representative archived program detail route, plus `robots.txt` and
  `sitemap.xml`, through direct HTTP requests. All returned the identical 403
  load-balancer response.
- Confirmed through search-indexed first-party results that archived concrete
  performances contain dates, times, venues, descriptions, and programme text,
  but those cached excerpts do not expose a complete catalogue or reliable API.
- Attempted to locate archived application JavaScript to recover API routes, but
  the archive index was unavailable and no current site bundle could be loaded.

## What would unblock implementation

Allowlisting the crawler egress address or otherwise permitting ordinary GET
requests from the crawler environment would expose the site application and its
network calls. Alternatively, first-party documentation for a stable public
program API (including category IDs, pagination, archives, performances, venues,
and detail descriptions) would allow an API-backed crawler. Once accessible, the
mixed source's full category taxonomy must be checked against the inclusion
guidance; unless a comprehensive stable set of eligible first-party filters can
be verified, the resulting candidate feed should use `upload_target="potential"`.
