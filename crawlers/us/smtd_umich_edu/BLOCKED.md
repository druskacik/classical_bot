<!-- crawler-factory-metadata
{"url":"https://smtd.umich.edu/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Access blocked

## Original URL

https://smtd.umich.edu/

The University of Michigan School of Music, Theatre & Dance is based in Ann Arbor, Michigan, so the resolved geography is the United States (`US`). Its calendar includes performances at University of Michigan venues and is not a genuinely multi-country source.

## Why a crawler cannot currently be implemented

Cloudflare returns HTTP 403 challenge pages to automated browser and ordinary HTTP clients for the home page, current event calendar, event archive, event detail infrastructure, API routes, feeds, and sitemaps. A production crawler would therefore be unable to retrieve either listings or details reliably. The site does publish current and archived events—search-engine results show dated performances with titles, times, venues, and descriptions—so this is an access failure rather than an empty calendar.

The source is mixed: its calendar covers classical music, opera, musical theatre, theatre, dance, jazz, electronic music, exhibitions, demonstrations, and other school events. Indexed calendar content exposes first-party filter controls for `Ensembles`, `Venues`, `Event Type`, `Cost`, and `Viewing Options`. Observed event-type values include `Performance`, `Recital | Studio`, `Recital | Faculty`, and `Exhibition`; observed ensemble values include orchestras, choirs, bands, contemporary music, jazz, gamelan, percussion, and glee clubs. Because the live application and its requests never became accessible, stable filter identifiers, combined-filter behavior, pagination persistence, date-range behavior, adjacent-filter coverage, and contamination could not be verified. An unfiltered feed must not be uploaded directly as classical.

## Approaches attempted

- Loaded `https://smtd.umich.edu/`, `/events/`, and the `/event/` archive using Playwright and inspected the network requests. Navigation received HTTP 403 and exposed only Cloudflare challenge traffic, not an event API request.
- Tested the WordPress REST API at `/wp-json/`, `/wp-json/wp/v2/types`, and `/wp-json/wp/v2/event?per_page=5`, plus the alternate `?rest_route=/wp/v2/types` route. Every route received HTTP 403.
- Tested likely machine-readable fallbacks at `/event/feed/`, `/events/feed/`, `/sitemap_index.xml`, `/event-sitemap.xml`, and `/wp-sitemap-posts-event-1.xml`. Feeds and sitemaps received HTTP 403. Only `/robots.txt` was accessible, and it merely points to the blocked sitemap index.
- Repeated representative HTML and REST requests with a normal browser user agent through a Python HTTP client. They also received HTTP 403 challenge HTML.
- Reviewed indexed current-calendar, paginated-calendar, archive, and performance-season results. These establish that concrete current and past events exist and reveal the visible filter labels and example values, but cached search excerpts are neither complete nor a stable first-party endpoint suitable for a universal crawler.

## What would unblock implementation

Any stable first-party access path that is permitted for the production crawler would unblock the work, for example:

- allowing the crawler's traffic through Cloudflare for public calendar and detail pages;
- publishing an accessible JSON, RSS, iCalendar, or other structured event feed; or
- providing the calendar API endpoint and any non-secret access requirements intended for automated public use.

Once access is available, the calendar's network requests should be inspected first. Because this is a mixed source, the first-party ensemble and event-type filters must be tested in combination across pagination and date ranges. If those filters cannot comprehensively isolate all project-eligible classical, opera, qualifying dance, crossover, and related performances, the crawler should collect the appropriate candidate performance feed with `upload_target="potential"`.
