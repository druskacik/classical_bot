<!-- crawler-factory-metadata
{"url":"https://philharmonia.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Access blocked

## Original URL

https://philharmonia.org/

The source is Philharmonia Baroque Orchestra & Chorale, a United States
organization whose calendar includes its Bay Area performances and US tours.
The resolved geography is therefore country scope, `US`.

## Why a crawler cannot currently be implemented

Cloudflare returns an HTTP 403 challenge page to both Playwright and a normal
Python `requests` client. The challenge is applied before the site's event
calendar or structured event API can be read. Shipping a crawler against this
source would consequently produce no records or fail on every run. Search-engine
caches show that concrete current, future, and past concerts exist, so this is
not an empty-calendar case, but cached search results are not a stable or
complete first-party source suitable for a production crawler.

## Approaches attempted

- Opened the canonical homepage with Playwright and inspected its network
  requests. Only Cloudflare challenge traffic loaded; no application API call
  was exposed.
- Requested the WordPress/The Events Calendar endpoint directly in Playwright:
  `/wp-json/tribe/events/v1/events?per_page=50&start_date=2025-01-01%2000:00:00`.
  It returned the same HTTP 403 challenge rather than JSON.
- Requested that API with Python `requests`, including a browser user agent;
  it also returned HTTP 403 HTML.
- Tested HTML calendar routes, including the first-party `concerts` category
  and its past-event view (`eventDisplay=past`). Both were challenged before
  event HTML was returned.
- Tested `/event-sitemap.xml` and `/wp-sitemap.xml`; both were also challenged.
- Tested the `www` hostname for the API; it resolved to the same protected
  source and returned HTTP 403.

Search-indexed first-party pages expose category values including `concerts`,
`sessions`, `special-concerts`, `tour`, `receptions`, and `gala`. Representative
indexed results indicate that `concerts`, `sessions`, `special-concerts`, and
`tour` contain qualifying performances, while `receptions` and `gala` contain
substantial non-concert contamination (donor meals/receptions and fundraising
events). Because the live pages and API could not be accessed, these filters
could not be verified for stable API identifiers, pagination persistence,
complete coverage, overlap, or adjacent-category omissions. No feed or upload
target was selected.

## What would unblock implementation

Any stable first-party access path that works for unattended clients would
unblock the crawler: allowlisting the production crawler, relaxing the
Cloudflare rule for the event API/calendar/sitemaps, or publishing an
unprotected JSON, ICS, RSS, or complete HTML event feed. Once available, the
event taxonomy and pagination must be rechecked before choosing between a
comprehensive filtered classical feed and `upload_target="potential"`.
