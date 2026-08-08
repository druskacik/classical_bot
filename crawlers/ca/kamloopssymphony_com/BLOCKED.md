<!-- crawler-factory-metadata
{"url":"https://kamloopssymphony.com/","geographic_scope":"country","country_code":"CA","reason_code":"access_blocked","attempted_at":"2026-08-08","retry_after":"2026-09-07"}
-->

# Crawler blocked by Cloudflare

Original URL: https://kamloopssymphony.com/

Kamloops Symphony is a Canadian classical-music organization, and its event
calendar still publishes concerts. A crawler cannot currently be implemented
because Cloudflare returns an HTTP 403 challenge page to every concert source
tested. The browser challenge does not resolve, so neither structured event
data nor concert HTML is available to a production crawler.

## Approaches attempted

- Loaded the homepage with Playwright and inspected its network traffic. The
  only dynamic requests were Cloudflare challenge requests; no event API was
  exposed. Waiting for the challenge did not grant access.
- Tested the WordPress and The Events Calendar REST API patterns, including
  `/wp-json/`, `/wp-json/tribe/events/v1/events`, the `rest_route` equivalent,
  and the `wp/v2/tribe_events` routes. All returned the same HTTP 403 challenge
  HTML instead of JSON.
- Tested HTML sources including the current event calendar, common event and
  season routes, individual concert pages, the `www` hostname, legacy season
  pages, and a past-events calendar query. All were challenged.
- Checked the sitemap endpoint, which was also challenged. Only `robots.txt`
  was directly readable; it contains no concert data.
- Confirmed through public search indexing that the event calendar has current
  and upcoming concert records, so this is an access problem rather than an
  empty calendar.

## What would unblock implementation

Any stable machine-readable source that is allowed through Cloudflare would
unblock the crawler: for example, allowlisting the crawler runtime, exempting a
read-only calendar/API route from the managed challenge, providing an event
feed (JSON, iCalendar, RSS, or XML), or supplying authorized API credentials.
Once access is available, the The Events Calendar REST API should be checked
first because its conventional endpoint appears to be the most likely source
of complete structured event and venue data.
