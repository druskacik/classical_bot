<!-- crawler-factory-metadata
{"url":"https://my.austinsymphony.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-16","retry_after":"2026-09-15"}
-->

# Access blocked

## Original URL

https://my.austinsymphony.org/

This is the first-party ticketing site for the Austin Symphony Orchestra in
Austin, Texas, so the resolved geographic scope is the United States (`US`).
The source is an orchestra calendar and appears classical-only; its indexed
events include Masterworks, orchestral pops and film-score concerts, chamber
events, and family performances that are within the project's inclusion scope.

## Why a crawler cannot currently be implemented

Imperva places an hCaptcha "Additional security check is required" page in
front of the event catalogue and event detail pages. The challenge is returned
with HTTP 200, so a normal production HTTP client would receive security HTML
instead of concert data. The challenge cannot be treated as a stable or
automatable source, and no accessible complete alternative feed was found.

Search-engine-indexed first-party pages confirm that concerts exist, including
future 2026–27 performances, but search results are neither a first-party feed
nor a complete, deterministic catalogue and therefore cannot support a
universal crawler.

## Investigation performed

- Playwright navigation to `/`, `/events`, and `/events?view=list` reached the
  Imperva/hCaptcha page. The captured network traffic contained only the
  requested document, Imperva resources, the challenge POST, and hCaptcha
  assets; the protected calendar application never loaded, so it exposed no
  reconstructable event API request.
- Direct HTTP requests tested `/events?view=list`, a representative overview
  page (`/overview/1225`), and `/sitemap.xml`. Each returned the Imperva
  security document rather than parseable catalogue or event HTML.
- API probes tested `/api`, `/api/events`, `/api/v1/events`, `/api/calendar`,
  `/api/performances`, and Swagger-style paths. `/api/events` and
  `/api/v1/events` returned structured 404 responses; the other useful paths
  were absent, blocked, or did not expose event data.
- `/robots.txt` was accessible and listed internal application areas, but it
  did not advertise a concert feed or sitemap that bypasses the protection.
- Indexed first-party event and overview pages were inspected to verify the
  site's identity, geography, current/future event coverage, event structure,
  and classical-only character. They expose labels such as `Masterworks
  Series` and `Special Event` on individual indexed pages, but the protected
  catalogue prevented testing any first-party category/filter values or their
  persistence across pagination and date ranges.

No applicable stable genre, category, discipline, event-type, series, or tag
filter could be exercised. Consequently, no filtered or unfiltered feed was
selected and no upload target can safely be implemented yet. If access becomes
available, the source's classical-only nature would ordinarily support the
`classical` upload target, subject to representative checks of the live feed.

## What would unblock implementation

Any one of the following would make a production crawler feasible:

- removal or allow-listing of automated read-only access to the catalogue and
  detail pages;
- a documented, unauthenticated first-party event API, JSON feed, RSS/Atom
  feed, or complete sitemap that is not behind the challenge; or
- stable server-rendered catalogue and detail HTML accessible to the
  repository's production HTTP client without solving a CAPTCHA.
