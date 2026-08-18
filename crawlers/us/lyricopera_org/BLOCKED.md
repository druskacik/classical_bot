<!-- crawler-factory-metadata
{"url":"https://www.lyricopera.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-18","retry_after":"2026-09-17"}
-->

# Access blocked

## Original URL

https://www.lyricopera.org/

## Why a crawler cannot currently be implemented

Lyric Opera of Chicago publishes a current 2026/27 performance catalogue, but
the site returns a Cloudflare `403 Attention Required` challenge to both an
interactive Playwright browser session and direct HTTP requests from the
crawler environment. The challenge is returned for the homepage, upcoming
shows page, production routes, and sitemap route, before any application HTML
or event data is delivered. A production crawler therefore cannot discover or
parse the catalogue reliably from this environment.

## Approaches attempted

- Opened `https://www.lyricopera.org/` with Playwright and inspected its network
  activity first. Only the Cloudflare block page loaded, so there were no
  first-party application API requests to reconstruct.
- Requested the homepage, `/shows/upcoming/`, `/productions/`, and
  `/sitemap.xml` directly with a browser user agent. Every route returned the
  same HTTP 403 Cloudflare response.
- Verified through indexed first-party URLs that the source still exposes
  concrete show pages and individual dated performances, including 2026/27
  opera, orchestral, choral, recital, live-to-film, crossover, musical, and
  special-event productions. Search-engine results are neither a first-party
  feed nor a complete and stable discovery interface, so they cannot safely be
  used as the crawler's data source.
- Looked for discoverable first-party API, genre, category, discipline,
  event-type, series, and tag endpoints. None could be tested because the
  Cloudflare response prevents the site application and its requests from
  loading. Consequently, filter identifiers, pagination behavior, historical
  coverage, and adjacent-category contamination could not be verified.

## What would unblock implementation

Allowlisting the production crawler egress IP, providing a stable first-party
JSON/calendar feed, or changing the Cloudflare policy so normal read-only
requests can retrieve the upcoming/archive listing and detail pages would
unblock implementation. Once access is available, the listing and relevant
adjacent event categories must be inspected across pagination and date ranges
before choosing between the `classical` and `potential` upload targets.
