<!-- crawler-factory-metadata
{"url":"https://virginiasymphony.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Virginia Symphony Orchestra crawler blocked

## Original URL

https://virginiasymphony.org/

The source is the US-based Virginia Symphony Orchestra. Its published calendar contains current and archived concrete performances, but automated access from the crawler environment is intercepted by a SiteGround JavaScript robot challenge. Every attempted request returns the challenge rather than event data, so a production crawler cannot currently retrieve or validate required event fields reliably.

## Approaches attempted

- Investigated the public calendar and representative indexed event pages. The site uses WordPress and The Events Calendar, with list, month, day, category, detail, and archive routes. Indexed pages show dates, times, named venues, cities, descriptions, and repertoire.
- Tested the expected first-party REST API at `/wp-json/tribe/events/v1/events`, including pagination parameters. It returns the same SiteGround challenge instead of JSON.
- Tested the WordPress `rest_route` form of that API and the WordPress REST index. Both are challenged.
- Tested the calendar's iCalendar endpoint and date-addressable HTML list/archive routes. Both are challenged.
- Tested the canonical and `www` hosts with browser-like and crawler user agents. The behavior is unchanged.
- Tested a real headless Chromium session so the JavaScript challenge could execute. It remained on the robot challenge and did not expose the calendar or API payload.
- Confirmed through indexed calendar pages that first-party categories exist, including `25-26-season`, `featured`, `pops`, `free-event`, and `virginia-arts-festival`. Category routes paginate, but stable identifiers and complete coverage could not be verified directly because access is blocked. Indexed samples also show contamination: `free-event` includes pre-concert panel discussions, while the unfiltered calendar includes orchestra concerts, opera, and other collaborations. Consequently, an eventual unfiltered implementation should use `upload_target="potential"` unless a comprehensive combination of stable first-party filters can be directly verified.

## What would unblock implementation

Allowlisting the crawler's production egress in SiteGround, disabling the challenge for read-only calendar/API routes, or providing an accessible first-party event feed would unblock implementation. Once access is available, the preferred route is the structured The Events Calendar REST API, with HTML detail parsing as a fallback. Pagination, historical date ranges, category coverage, location fields, and representative adjacent-category records must then be tested before selecting the final feed and upload target.
