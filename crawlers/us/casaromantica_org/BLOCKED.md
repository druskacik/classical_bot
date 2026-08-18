<!-- crawler-factory-metadata
{"url":"https://www.casaromantica.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-18","retry_after":"2026-09-17"}
-->

# Casa Romantica crawler blocked

## Original URL

https://www.casaromantica.org/

Casa Romantica Cultural Center and Gardens is based in San Clemente, California,
so the resolved geography is the United States (`US`).

## Why implementation is currently blocked

The site is protected by a Cloudflare JavaScript challenge. Every directly
tested first-party route returned HTTP 403 and a `Just a moment...` challenge
document rather than event data. A crawler built on those responses would not
work in the production requests-based crawler environment.

Search-engine results confirm that the site publishes concrete events and past
event pages, but a search index is neither a complete nor a stable paginated
source and cannot support a universal crawler.

## Approaches attempted

- Requested the main upcoming-events calendar and the `Live Music` category
  archive as HTML, with normal browser and crawler user agents.
- Tested the WordPress REST API at `/wp-json/` and the likely Modern Events
  Calendar post endpoint `/wp-json/wp/v2/mec-events`, including the alternative
  `?rest_route=/wp/v2/mec-events` form.
- Tested likely event RSS and sitemap discovery routes:
  `/events/feed/`, `/wp-sitemap-posts-mec-events-1.xml`, and
  `/event-sitemap.xml`.
- Tested both `www` and apex-domain access, plus HTTP-to-HTTPS navigation.
- Inspected indexed calendar, category, and representative event-detail pages.
  The first-party `Live Music` category is not an in-scope classical filter: it
  contains classical chamber concerts alongside jazz, folk, Native American
  dance/music, coffee concerts of unclear repertoire, and contemporary fusion.
  Eligible youth chamber events can additionally carry `Casa Kids`. Therefore,
  even if accessible, the selected feed would need to be the broad concrete
  event feed with `upload_target="potential"`; filtering only `Live Music` or
  titles containing “classical” would be incomplete and contaminated.

No stable API/category identifiers could be tested across pagination or date
ranges because the challenge intercepted every live request. The indexed pages
show the relevant category value `Live Music`, plus adjacent `Casa Kids`, `Live
Theater and Dance`, and general calendar listings, but do not expose a reliable
pagination transport.

## What would unblock implementation

Any one of the following would allow a crawler to be implemented and validated:

- Cloudflare allowing the production crawler user agent/IP to read public HTML
  and WordPress endpoints;
- a documented, unchallenged first-party calendar/API/feed endpoint;
- a stable server-rendered calendar mirror containing detail URLs, dates,
  venues, and pagination; or
- browser automation available in the production crawler runtime, together with
  permission to use it and a repository-supported browser dependency.

