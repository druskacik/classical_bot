<!-- crawler-factory-metadata
{"url":"https://wagonwheelcenter.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Access blocked

## Original URL

https://wagonwheelcenter.org/

The source is Wagon Wheel Center for the Arts in Warsaw, Indiana, so its resolved geography is the United States (`US`). The site publishes a mixed performing-arts calendar rather than a classical-only feed.

## Why a crawler cannot currently be implemented

Every tested first-party page and endpoint is intercepted by a hosting-provider robot challenge. The origin returns HTTP 202 with a short HTML meta-refresh to `/.well-known/sgcaptcha/`; Playwright then reaches a page titled `Robot Challenge Screen` that says it is checking connection security. A normal production `requests` crawler therefore cannot obtain listing HTML or structured event data, and implementing against search-engine snippets would not be a stable first-party crawler.

## Approaches attempted

- Opened the canonical home page with Playwright and inspected its network requests. The only first-party requests were the challenged home request and the `/.well-known/sgcaptcha/` challenge page; no event API request was exposed.
- Tested the likely WordPress REST discovery endpoint `/wp-json/`.
- Tested The Events Calendar REST endpoint `/wp-json/tribe/events/v1/events?per_page=5`.
- Tested the WordPress event collection endpoint `/wp-json/wp/v2/tribe_events?per_page=5`.
- Tested list pagination at `/events/?tribe_paged=1&tribe_event_display=list` and `/events/?tribe_paged=2&tribe_event_display=list`.
- Tested `/wp-sitemap.xml` as a possible archive/detail-page discovery source.
- Reviewed indexed first-party listing and detail-page results to confirm that concerts do exist and that the calendar includes orchestra, theatre, musicals, competitions, and non-event season subscriptions. Indexed pages identify WordPress/The Events Calendar-style listings, but they are not adequate as a scrape source and do not expose a sufficiently comprehensive stable classical filter.

All direct API and HTML requests above returned the same HTTP 202 challenge shell. Consequently, no first-party genre, category, discipline, event-type, series, or tag filter values could be enumerated or tested across pagination. No filtered feed was selected, and no upload target can responsibly be configured yet. Given the mixed calendar, an eventual crawler should use `upload_target="potential"` unless stable first-party filters can be verified as comprehensive and uncontaminated.

## What would unblock implementation

Any of the following would permit a retry:

- allowlisting the crawler's production egress IP or disabling the robot challenge for read-only event and REST routes;
- a stable first-party JSON, RSS, iCalendar, or HTML calendar endpoint that is exempt from the challenge;
- documented API credentials or another supported first-party data feed.

Once access is available, the WordPress and The Events Calendar endpoints above should be rechecked first, including category/tag enumeration, past-event date ranges, pagination persistence, representative adjacent categories, concrete occurrence expansion for multi-day productions, and exclusion of subscriptions or season overview records.
