<!-- crawler-factory-metadata
{"url":"https://eastdevonmusic.org/","geographic_scope":"country","country_code":"GB","reason_code":"access_blocked","attempted_at":"2026-08-19","retry_after":"2026-09-18"}
-->

# East Devon Music crawler blocked

## Original URL

https://eastdevonmusic.org/

East Devon Music is a UK classical-music organisation whose indexed “What’s On” calendar publishes concrete concerts in Devon. The resolved geography is therefore Great Britain (`GB`), not multi-country.

## Why implementation is currently blocked

Every direct request is intercepted by StackProtect bot verification instead of returning site or calendar data. The canonical host returns an HTTP 401 JavaScript-and-cookie challenge; the `www` host returns HTTP 403. This protection also covers the site’s machine-readable endpoints. A production crawler based on the currently observable responses would only parse the verification page and could not reliably locate concerts.

The unavailable content cannot be replaced with search-engine snippets: although indexing confirms that `/whats-on/` recently contained a concrete ISCA Ensemble Southwest event, snippets are neither a complete catalogue nor a stable first-party scraping interface.

## Approaches attempted

- Browser/network investigation could not be completed because this crawler-factory session did not expose a Playwright MCP or another browser tool.
- Requested the canonical home page and followed redirects with browser-like headers, compression, and cookie persistence. Responses remained StackProtect verification pages (HTTP 401/403).
- Tested the first-party The Events Calendar REST route `/wp-json/tribe/events/v1/events` and the WordPress event collection `/wp-json/wp/v2/tribe_events`; both returned the same HTTP 401 challenge.
- Tested `/events/feed`, `robots.txt`, `sitemap_index.xml`, `event-sitemap.xml`, and `wp-sitemap.xml`; all were protected by the same challenge.
- Inspected public search indexing for the home page, `/about/`, `/whats-on/`, dated pages, and event terms. It confirms a classical-only calendar but does not expose a complete or dependable archive.

## Filters and feed assessment

No applicable first-party genre, category, discipline, event-type, series, or tag filter could be inspected because the calendar HTML and API are access-blocked. Consequently no filter identifiers, pagination behavior, date-range behavior, adjacent categories, or archive coverage could be verified. The likely The Events Calendar API feed could not be selected because its response is also blocked. If access becomes available, the source appears classical-only and may qualify for `upload_target="classical"`, but that must be confirmed from representative accessible listing and detail pages before implementation.

## What would unblock implementation

Any of the following would permit a reliable crawler:

- allowlisting the crawler runtime or removing the StackProtect challenge from public calendar/API paths;
- a documented first-party JSON, ICS, or RSS feed that is accessible without interactive verification;
- an accessible browser MCP session able to complete the verification, followed by confirmation that any resulting API access also works non-interactively in the production crawler runtime.
