<!-- crawler-factory-metadata
{"url":"https://www.fondazionepergolesispontini.com/","geographic_scope":"country","country_code":"IT","reason_code":"access_blocked","attempted_at":"2026-08-13","retry_after":"2026-09-12"}
-->

# Access blocked

## Original URL

https://www.fondazionepergolesispontini.com/

The source is the Italian Fondazione Pergolesi Spontini and publishes its own
performances primarily in Italy, so the resolved geography is country scope,
Italy (`IT`).

## Why a crawler cannot currently be implemented

Cloudflare presents an active browser-verification challenge and returns HTTP
403 for the homepage, the event catalogue, pagination, event detail access,
WordPress REST endpoints, feeds, and sitemaps. The challenge did not resolve in
the Playwright browser session. A production crawler based on the repository's
HTTP interfaces would therefore be unable to retrieve either the catalogue or
the detail pages reliably.

Search-engine indexing confirms that concrete current and archived events do
exist under `/eventi/`, including paginated listings such as `?pno=2`, but
search-engine snippets are neither a complete nor a stable first-party source
and cannot support a universal crawler.

## Approaches attempted

- Loaded the homepage with Playwright and waited for the Cloudflare challenge;
  the document remained a `403 Just a moment...` response.
- Inspected Playwright network traffic. It contained only Cloudflare challenge
  requests and no application event API request that could be reconstructed.
- Requested the first-party event catalogue at `/eventi/` and pagination at
  `/eventi/?pno=2`; both returned the same Cloudflare 403 page.
- Tested the WordPress REST index at `/wp-json/` and `?rest_route=/`; both were
  blocked with HTTP 403.
- Tested `wp-sitemap.xml`, `sitemap_index.xml`, and `/feed/`; all were blocked
  with HTTP 403. Only `robots.txt` was accessible and it does not expose event
  data or an alternative API.
- Tested both the `www` and apex hostnames; both were blocked.
- Examined indexed representative pages. The site's main event feed is mixed:
  it includes eligible opera, orchestral and chamber concerts, cine-concerts,
  family classical events, and musicals, but also pop, jazz, ordinary theatre,
  escape-room and other non-concert records. No accessible first-party genre,
  category, discipline, event-type, series, or tag filter could be tested.

Because filters and their pagination behavior could not be accessed, there is
no defensible filtered classical feed. If access were restored, the appropriate
initial choice would be the complete paginated `/eventi/` candidate feed with
`upload_target="potential"`, unless stable and comprehensive first-party filter
identifiers could then be verified across pagination and archives.

## What would unblock implementation

Any stable first-party access path that works for unattended server requests
would unblock the crawler, for example:

- allowlisting the crawler infrastructure or removing the challenge from event
  catalogue, detail, pagination, and API routes;
- an accessible WordPress REST or Events Manager API endpoint;
- a complete first-party JSON, XML, RSS, or iCalendar event export; or
- reproducible non-expiring request credentials explicitly intended for
  automated access.

Once available, the event feed and adjacent categories must be checked against
the project inclusion guidance, including opera, ballet/classical dance,
contemporary art music, cine-concerts, crossover, qualifying musicals, and
family concerts.
