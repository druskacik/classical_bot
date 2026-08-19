<!-- crawler-factory-metadata
{"url":"https://earlyopera.com/","geographic_scope":"country","country_code":"GB","reason_code":"access_blocked","attempted_at":"2026-08-19","retry_after":"2026-09-18"}
-->

# Early Opera Company crawler blocked

## Original URL

https://earlyopera.com/

## Why a crawler cannot currently be implemented

The canonical site and all tested first-party discovery and data endpoints are protected by StackProtect. They return the same HTTP 401 Security Verification page to both a real Playwright browser and ordinary HTTP clients. The verification page's challenge request returns HTTP 403, so there is no stable first-party response that a production crawler can parse.

Search-engine results confirm that the British Early Opera Company still publishes concrete upcoming and archived classical performances under `/whats-on/` and `/concerts/...`; this is therefore an access block, not an empty calendar or a repurposed domain. The organization is UK-based and tours occasionally, so the resolved geography remains country scope with `GB` as the crawler country.

## Approaches attempted

- Opened the canonical home page with Playwright and inspected its network requests. The document returned HTTP 401 and the only dynamic request was a `/.stackprotect/...json` challenge request, which returned HTTP 403. No event API request was made.
- Probed the likely first-party HTML calendar paths `/whats-on/`, `/events/`, `/concerts/`, and `/past-events/`.
- Probed WordPress/API discovery paths including `/wp-json/`, `/wp-json/wp/v2/types`, `/wp-json/wp/v2/search`, and `?rest_route=/wp/v2/concerts`.
- Probed discovery/archive sources including `/robots.txt`, `/sitemap.xml`, `/wp-sitemap.xml`, `/wp-sitemap-posts-concerts-1.xml`, and `/feed/`.
- Tried the HTTP-to-HTTPS route, the `www` hostname, browser-like and crawler user agents. All usable first-party paths ultimately returned the same HTTP 401 verification document.
- Checked indexed representative pages only to establish that `/whats-on/` contains upcoming and past event occurrences and that `/concerts/...` detail pages contain dates, event type, venue, programme/description, and performers. Search results are not a sufficiently complete or stable first-party feed for a production crawler.

## Filters and upload-target assessment

No applicable first-party genre, category, discipline, event-type, series, or tag filters could be inspected because every first-party HTML and API endpoint is behind the verification layer. Indexed pages indicate that the source is the classical-only calendar of a Baroque opera ensemble, so a working first-party feed would likely qualify for `upload_target="classical"`; this could not be validated across pagination or date ranges while access is blocked.

## What would unblock implementation

Any stable server-readable first-party route would be sufficient: removal or allow-listing of the StackProtect challenge for the calendar and detail pages, an accessible WordPress REST endpoint or sitemap/feed, or a documented public event API. Once available, the calendar should be checked for complete past-event pagination and touring locations before implementing the crawler.
