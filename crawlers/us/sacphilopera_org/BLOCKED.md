<!-- crawler-factory-metadata
{"url":"https://www.sacphilopera.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-19","retry_after":"2026-09-18"}
-->

# Sacramento Philharmonic & Opera crawler blocked

## Original URL

https://www.sacphilopera.org/

## Why a crawler cannot currently be implemented

The source is active and publishes classical concerts and opera in Sacramento,
California, but Cloudflare returns HTTP 403 challenge pages to automated access.
The challenge did not resolve in a JavaScript-enabled Playwright browser. The
same protection blocks the site's event HTML, discovery files, WordPress API,
and separate first-party ticketing host, so there is no production-accessible
listing or detail source from which a working crawler can be built and tested.

Search-engine results confirm that concrete 2026-2027 production pages exist,
but a search index is not a stable or complete first-party feed and cannot
support a universal crawler.

## Approaches attempted

- Loaded the home page in Playwright and inspected its network requests. Only
  the Cloudflare challenge flow was exposed; no event API request was made.
- Waited for the browser challenge to resolve, but the response remained HTTP
  403.
- Requested likely HTML/discovery paths (`/events/`, `/performances/`,
  `/robots.txt`, and `/sitemap.xml`) in Playwright; all returned the same
  challenge.
- Requested WordPress discovery and API routes (`/wp-json/`,
  `/wp-json/wp/v2/types`, `/wp-json/wp/v2/production?per_page=10`, and
  `/wp-json/wp/v2/search?search=Trovatore`); all were blocked before any JSON
  response. Consequently, pagination and date-range behavior could not be
  tested.
- Tried the separate first-party ticketing host at
  `https://tickets.sacphilopera.org/sacphilopera/website/`; Cloudflare also
  returned HTTP 403.
- Confirmed with ordinary HTTP requests that the canonical, apex, HTTP, and
  simple alternate-query URLs all return Cloudflare 403 responses.

The organization is a US-based classical-only source. No applicable genre,
category, discipline, event-type, series, or tag filter could be inspected or
tested because all first-party feeds are blocked. If access becomes available,
the production catalogue (including archived productions) should be preferred
and may upload directly to `classical`; any broader or incompletely filtered
feed should instead use `potential`.

## What would unblock implementation

Any stable first-party source accessible to the production crawler would
unblock this work, such as allowlisting the crawler, relaxing the Cloudflare
challenge for public listing/API routes, or documenting an accessible event
API or calendar feed. That source must expose discoverable occurrences and
detail data so dates, times, venues, descriptions, pagination, archives, and
coverage can be validated.
