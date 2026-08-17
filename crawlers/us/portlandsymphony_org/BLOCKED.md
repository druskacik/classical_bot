<!-- crawler-factory-metadata
{"url":"https://portlandsymphony.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Crawler blocked

Original URL: https://portlandsymphony.org/

The source is the Portland Symphony Orchestra in Portland, Maine, United States. A production crawler cannot currently extract complete, valid concert records because Cloudflare returns an HTTP 403 challenge page for the event listing, individual event pages, and WordPress REST event endpoints. The challenge also does not complete in the available headless Chromium session. The publicly accessible sitemap contains event URLs and last-modified timestamps, but it does not contain occurrence dates, times, venues, cities, or descriptions, so sitemap-only records would be invalid.

## Approaches attempted

- Network/API: inspected the WordPress REST API discovery endpoint and post-type routes; probed `/wp-json/wp/v2/event`, `/wp-json/wp/v2/events`, the `rest_route` equivalent, and The Events Calendar-style `/wp-json/tribe/events/v1/events`. Event data requests were challenged or unavailable.
- First-party filters: inspected the exposed taxonomy sitemap. Event-type values are `2526-season`, `2627-season`, `concert`, `family-programs`, `magic-of-christmas`, `pso-offstage`, `special-events`, and `non_concert_homepage_slider`. Because listing/detail responses are blocked, filter contents, pagination persistence, eligible adjacent categories, and contamination cannot be verified safely.
- HTML: requested the home page, `/events/`, likely concert-listing paths, representative current event detail pages, and archive detail pages. Cloudflare returned an HTTP 403 challenge page.
- Browser: attempted the representative event detail page in a real headless Chromium session; the challenge did not resolve and no usable DOM was returned.
- Sitemaps: successfully inspected `sitemap_index.xml`, `event-sitemap.xml`, and the event taxonomy sitemap. The event sitemap exposes a large current-and-past URL archive, but lacks the required concert fields.

## What would unblock implementation

Reliable first-party access to event detail HTML or a stable event API/feed that exposes occurrence dates, times, venue/city, and descriptions would allow implementation. This could be provided by allowlisting the crawler runtime, changing the Cloudflare policy for public event/API routes, or exposing a public structured calendar feed. Once accessible, all performance-bearing PSO categories—including family and holiday concerts—must be checked rather than relying only on the narrow `concert` taxonomy.
