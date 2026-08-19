<!-- crawler-factory-metadata
{"url":"https://www.springfieldsymphony.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-19","retry_after":"2026-09-18"}
-->

# Springfield Symphony Orchestra crawler blocked

## Original URL

https://www.springfieldsymphony.org/

This is the Springfield Symphony Orchestra in Springfield, Massachusetts, so
the resolved geography is the United States (`US`).

## Why a crawler cannot currently be implemented

The origin intercepts all tested requests with a SiteGround BotDetect robot
challenge. The challenge remained active in a JavaScript-capable Playwright
browser, did not set an access cookie, and redirected from the initial
`/.well-known/sgcaptcha/` page to `/.well-known/captcha/`. Consequently neither
the event catalogue nor individual detail pages are available to a production
HTTP crawler from this environment.

Search-engine results confirm that the site publishes concrete Springfield
Symphony Orchestra performances using WordPress and The Events Calendar, but a
search index is neither a complete nor a sufficiently current first-party feed
for a production crawler.

## Approaches attempted

- Loaded the home page in Playwright and inspected its network traffic. The
  only origin response was HTTP 202 followed by the robot challenge; no event
  API or calendar request was made.
- Allowed the challenge to run in Playwright for an additional 15 seconds. It
  remained on the challenge page and produced no usable cookie.
- Tested the WordPress REST discovery routes `/wp-json/` and
  `/wp-json/wp/v2/types`.
- Tested the expected The Events Calendar API routes
  `/wp-json/tribe/events/v1/events` and
  `/?rest_route=/tribe/events/v1/events`.
- Tested HTML calendar routes `/events/`, `/events/list/`, and the past-event
  query `/events/list/?tribe_event_display=past`.
- Tested The Events Calendar iCalendar forms `/events/?ical=1` and
  `/events/list/?ical=1`, plus `sitemap.xml` and `robots.txt`.
- Retried representative routes on the bare and `www` hosts, over HTTP and
  HTTPS, and with browser, crawler, search-engine, and social-preview user
  agents. The origin continued to return the challenge.

The first-party taxonomy values visible in indexed detail pages are exactly
`Classical Concert` and `Concerts`. The corresponding first-party category feed
`/events/category/concerts/` was identified and tested, but it too is blocked.
Because no event response could be obtained, category identifiers, adjacent
categories, past/future date ranges, and persistence across pagination could
not be verified. No feed or upload target can therefore be selected safely.

## What would unblock implementation

Allowlisting the crawler-factory and production crawler egress addresses in
SiteGround, disabling the challenge for public read-only calendar/API routes,
or providing another stable unauthenticated first-party event feed would allow
implementation. The preferred route to reassess first is The Events Calendar
REST endpoint; its pagination, date parameters, taxonomy identifiers, detail
coverage, and archive behavior must then be verified before choosing between a
filtered `classical` feed and a broader `potential` feed.
