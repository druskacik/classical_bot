<!-- crawler-factory-metadata
{"url":"https://www.thecip.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# Columbus Indiana Philharmonic crawler blocked

## Original URL

https://www.thecip.org/

The source is the Columbus Indiana Philharmonic, based in Columbus, Indiana,
United States. Its resolved geography is therefore country scope with ISO
country code `US`.

## Why implementation is currently blocked

Cloudflare returns an HTTP 403 security-verification page before any site or
event content is delivered. The challenge also applies to machine-oriented
WordPress endpoints, so a production crawler cannot currently retrieve a
stable first-party catalogue or event details. Creating a parser from stale
search-engine snippets would not provide complete coverage and would not be a
working universal crawler.

## Approaches attempted

- Loaded the canonical homepage with Playwright and inspected its network
  requests. Only Cloudflare challenge-platform and Turnstile traffic appeared;
  no application event API, GraphQL request, or structured event response was
  exposed.
- Tested the `www` and apex HTTPS hosts, plus the HTTP URL and its HTTPS
  redirect. All reached the same 403 verification page.
- Tested likely WordPress API forms: `/wp-json/`, `/wp-json/wp/v2/types`,
  `/wp-json/wp/v2/search?search=concert&per_page=100`, and
  `/?rest_route=/`. Cloudflare blocked each endpoint before JSON was returned.
- Tested HTML and discovery routes `/events/`, `/wp-sitemap.xml`, `/feed/`, and
  `/events/feed/`. Each route was blocked by the same challenge, preventing
  HTML parsing, archive traversal, filter discovery, and pagination checks.
- Search-engine results were reviewed only to establish source identity and
  the existence of concrete concert detail pages (for example, the archived
  `/events/harmony-glow/` page). They do not expose a complete, current,
  first-party feed suitable for crawling.

No applicable first-party genre, category, discipline, event-type, series, or
tag filters could be inspected, and no exact filter values or persistence
across pagination could be tested because application content was never
reachable. Consequently, no feed or upload target can be selected safely.

## What would unblock implementation

Any stable first-party route that permits automated access would unblock the
crawler: allowlisting the production crawler, relaxing the Cloudflare rule for
public event/API routes, or providing an accessible event feed or documented
API. Once access is available, the event archive and network traffic should be
rechecked for structured endpoints and complete scope filters before choosing
between `classical` and `potential` upload targets.
