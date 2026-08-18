<!-- crawler-factory-metadata
{"url":"https://www.mallarmemusic.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-18","retry_after":"2026-09-17"}
-->

# Mallarmé Music crawler blocked

## Original URL

https://www.mallarmemusic.org/

Mallarmé Music is a chamber-music organization based in Durham, North Carolina,
so the resolved geographic scope is the United States (`US`).

## Why implementation is currently blocked

Cloudflare returns an HTTP 403 managed-challenge page to both browser and normal
HTTP clients. The challenge does not resolve in the Playwright browser, and the
same protection covers the event listing, date-ranged archive, sitemap, and API
routes. Consequently there is no first-party response that a production crawler
can currently parse or test reliably.

Publicly indexed pages show that concerts exist in current and past calendar
views. They also show that the calendar is a mixed feed: concrete chamber-music
performances appear alongside subscription-package entries, calls for talent,
community participation events, and festival overview/pass pages. No applicable
first-party genre, category, discipline, event-type, series, or tag filter could
be tested because all live calendar and API requests are blocked. An unfiltered
feed therefore could not safely be uploaded as classical even if access were
available; absent a comprehensive stable filter, it would require the
`potential` upload target.

## Approaches attempted

- Loaded the homepage with Playwright and inspected its network traffic. The
  only dynamic requests were Cloudflare challenge requests; no application or
  event-data API request was exposed.
- Waited for the Cloudflare Turnstile/managed challenge to complete. The page
  remained on the HTTP 403 "Just a moment" response.
- Probed the likely WordPress and The Events Calendar endpoints, including
  `/wp-json/`, `/?rest_route=/`, and `/wp-json/tribe/events/v1/events`; each
  returned the Cloudflare challenge.
- Probed `/event/list/` with a `tribe-bar-date` value to test archive/date-range
  behavior; it was blocked before calendar pagination could be inspected.
- Probed `/sitemap.xml` and `/wp-sitemap.xml`; both were blocked. Only
  `/robots.txt` was accessible, and it contains no concert records.
- Checked publicly indexed current, historical, organizer, list, and photo-view
  calendar pages to confirm that the site publishes concrete concerts and
  archives, and to assess feed contamination. Search-index copies are not a
  stable first-party interface and are unsuitable as a crawler source.

## What would unblock implementation

Any stable first-party route that is accessible to unattended HTTP clients would
unblock the crawler, for example an allowlisted The Events Calendar REST API,
an accessible iCalendar export, or server-rendered event list/detail pages that
do not require the Cloudflare challenge. Access must include pagination and past
date ranges so archive coverage, event categories, concrete occurrence details,
and representative adjacent categories can be verified before selecting the
feed and upload target.
