<!-- crawler-factory-metadata
{"url":"https://www.ilsymphony.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-18","retry_after":"2026-09-17"}
-->

# Illinois Symphony Orchestra crawler blocked

## Original URL

https://www.ilsymphony.org/

## Why a crawler cannot currently be implemented

The Illinois Symphony Orchestra is a US-based classical-only organization with
published concert occurrences, but its Cloudflare configuration currently blocks
automated access to every first-party page and asset tested. Direct requests from
both Playwright and Python receive an HTTP 403 Cloudflare challenge page instead
of concert data. A production crawler therefore cannot retrieve or parse the
source reliably.

An unofficial third-party text-rendering proxy could read the site and confirmed
that concrete events exist, but depending on that proxy would not be a stable or
first-party scraping implementation.

## Approaches attempted

- Loaded the canonical homepage with Playwright and inspected its network
  requests. The only application request was the homepage returning HTTP 403;
  subsequent requests were Cloudflare challenge/Turnstile traffic. No concert
  API or structured event request was exposed.
- Tested the first-party calendar and its category query values:
  `https://www.ilsymphony.org/concerts-events`, `category=concerts`,
  `category=sips-sounds`, `category=around-the-town-concerts`, and
  `category=special-events`. These stable values are exposed by the site's own
  navigation, but the pages are blocked before pagination or date-range behavior
  can be verified.
- Tested ordinary HTML discovery endpoints (`/robots.txt`, `/sitemap.xml`) and
  likely WordPress API endpoints (`/wp-json/`, `/wp-json/wp/v2/`). All returned
  the same HTTP 403 Cloudflare challenge HTML rather than parseable content.
- Tested a known first-party PDF on `assets.ilsymphony.org`; that asset host is
  protected by the same challenge and returned HTTP 403.
- Used an external text-rendering proxy only as an investigation aid. It exposed
  representative concrete concert pages and occurrences (including Sips &
  Sounds and Around the Town concerts), confirming the source is not empty, but
  this was rejected as a production data source.

## What would unblock implementation

Allowing non-interactive read access to the public calendar and event detail
pages, or exposing a first-party JSON/RSS/iCalendar endpoint that is not behind
the Cloudflare challenge, would allow a crawler to be implemented. Once access
is restored, the category feeds must be checked across pagination and archives;
the concert-bearing `concerts`, `sips-sounds`, and
`around-the-town-concerts` feeds should be combined, while `special-events`
requires representative review because it may contain fundraising or other
non-concert records.
