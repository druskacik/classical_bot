<!-- crawler-factory-metadata
{"url":"https://filharmoniasudecka.pl/","geographic_scope":"country","country_code":"PL","reason_code":"access_blocked","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# Access blocked

## Original URL

https://filharmoniasudecka.pl/

## Why a crawler cannot currently be implemented

The domain resolves to `46.242.227.233`, but the origin did not return an HTTP
response from the crawler-factory environment. HTTPS and HTTP connections to
the canonical host timed out, as did HTTPS access through the `www` hostname.
The ticketing subdomain was also unreachable. Because no live response could be
retrieved, the event-list pagination, category parameters, API response schema,
detail-page markup, and handling of past events could not be tested reliably.

Search-engine results crawled shortly before this attempt show that the source
is still active and publishes a Polish calendar at `/repertuar-filharmonii/`.
Those results expose 42 events, past-event navigation, individual `/event/.../`
pages, and first-party category facets including `Koncert kameralny`, `Koncert
symfoniczny`, `Festiwal Księżnej Daisy`, `Filharmonia w Królewskiej`, `Koncert
Familijny`, `Poranki Muzyczne`, and `Wydarzenie zewnętrzne`. They also show at
least one jazz event in the wider calendar, so the source cannot safely be
treated as an unfiltered classical-only feed without inspecting category
behavior and representative detail pages.

## Approaches attempted

- Playwright navigation to the canonical homepage was attempted first. It
  timed out before `DOMContentLoaded`, and the browser could not return a
  network-request list because the backend remained stalled on the origin.
- Direct HTTPS requests to the canonical URL timed out without headers or a
  response body.
- Plain HTTP and the `www` HTTPS hostname were tested to rule out a scheme or
  hostname redirect issue; both timed out.
- The separately indexed ticketing subdomain was tested as a possible
  structured fallback and also timed out.
- Publicly indexed pages were inspected to identify the calendar technology,
  categories, event URL pattern, current coverage, and presence of past-event
  navigation. The markup is consistent with WordPress and The Events Calendar,
  so `/wp-json/tribe/events/v1/events` and WordPress REST routes were identified
  as likely API candidates, but the origin block prevented requesting or
  validating them.
- HTML parsing of the calendar and event-detail pages was considered, but no
  live HTML could be downloaded for selector and pagination validation.

## What would unblock implementation

Restore HTTP access to `filharmoniasudecka.pl` from the crawler environment, or
provide an allowlisted/mirrored first-party calendar endpoint. A retry should
first inspect the live Playwright network log for The Events Calendar REST
requests, then validate exact category identifiers across pages and past/future
date ranges. If the REST feed is unavailable, the calendar pagination and
individual event pages can be parsed from HTML once representative responses
are accessible.
