<!-- crawler-factory-metadata
{"url":"https://www.nromusic.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-19","retry_after":"2026-09-18"}
-->

# Crawler blocked: National Repertory Orchestra

## Original URL

https://www.nromusic.org/

## Why implementation is blocked

The National Repertory Orchestra is a United States-based classical orchestra and its site publishes concrete concert occurrences, including a 2026 calendar. However, the production site currently intercepts automated requests with a server-side "Robot Challenge Screen". The initial response is HTTP 202 challenge HTML, not the requested calendar, API payload, or feed. A normal Python `requests` crawler would therefore have no stable first-party event data to parse.

The browser challenge remained unresolved after JavaScript execution and waiting. Shipping a crawler against search-engine copies would not provide a reliable, complete, or first-party scrape and would not satisfy pagination or archive coverage requirements.

## Approaches attempted

- Loaded the homepage with Playwright and inspected its network traffic. Only the challenge document, challenge redirect, CloudFront challenge images, and browser blob resources appeared; no event API request was exposed.
- Waited for the proof-of-work challenge to complete in Playwright. It remained on the challenge page.
- Tested the first-party calendar HTML at `/event-calendar/`, its list/date form (`/event-calendar/list/?tribe-bar-date=2026-06-01`), and the site's event route. All returned challenge HTML.
- Tested WordPress REST discovery and search under `/wp-json/wp/v2/`. Both were challenge-protected.
- Tested The Events Calendar REST endpoint `/wp-json/tribe/events/v1/events?per_page=5`, including the `?rest_route=/tribe/events/v1/events` form and the non-`www` hostname. All returned challenge HTML.
- Tested The Events Calendar iCalendar forms (`/event-calendar/?ical=1` and `/event-calendar/list/?ical=1`). Both returned challenge HTML.
- Verified through currently indexed pages that the site has concrete 2026 orchestra concerts and exposes calendar taxonomies such as `category/ticketed-event` and `tag/free-event`, with multi-page results. These copies also show that `free-event` contains open rehearsals and potentially other adjacent activities, so it cannot safely be treated as a comprehensive concert-only filter without first-party detail-page checks. The challenge prevents verifying stable filter identifiers across live pagination and date ranges.

## Filters and feed assessment

The exact first-party taxonomy routes identified and attempted were `category/ticketed-event` and `tag/free-event`; the unfiltered calendar and date/list routes were also tested. No live filter could be validated because every first-party request was intercepted. Indexed evidence suggests the organization itself is classical-only, but the broad calendar includes donor events and rehearsals as well as concerts. Consequently, no feed or upload target can be selected safely at this time.

## What would unblock implementation

Any of the following would allow a crawler to be implemented:

- removal or server-side allowlisting of the crawler's access to the calendar;
- a stable first-party JSON, iCalendar, RSS, or HTML endpoint exempt from the robot challenge;
- documented challenge-compatible access intended for automated clients; or
- a first-party downloadable season calendar containing every event with stable detail URLs, dates, times, venues, and cities.

Once access is restored, the preferred investigation path is The Events Calendar REST API, followed by the iCalendar feed or paginated calendar HTML. Relevant taxonomy feeds and adjacent event types must then be checked before deciding between `classical` and `potential` upload.
