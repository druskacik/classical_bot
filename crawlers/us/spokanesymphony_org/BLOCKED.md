<!-- crawler-factory-metadata
{"url":"https://spokanesymphony.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Spokane Symphony crawler blocked

## Original URL

https://spokanesymphony.org/

The source is the US-based Spokane Symphony. The supplied domain currently points visitors toward the organization's Fox Theater Spokane site, but requests to both domains are intercepted by a SiteGround robot challenge in this environment.

## Why implementation is blocked

The server returns HTTP 202 and a small HTML page that redirects to `/.well-known/sgcaptcha/` instead of returning either the event page or API data. The challenge requires browser-side JavaScript and does not yield scrapeable concert content or a reusable server-side session. Consequently, event fields and first-party filters cannot be verified, and a production `requests` crawler would fail in the same way from the investigated environment.

## Approaches attempted

- Requested `https://spokanesymphony.org/` with redirects enabled.
- Investigated the first-party WordPress Events Calendar REST API advertised by the site: `https://foxtheaterspokane.org/wp-json/tribe/events/v1/`.
- Requested the API's `events`, `categories`, and `organizers` endpoints, including pagination parameters.
- Tried the WordPress `?rest_route=` form and both bare and `www` hostnames.
- Requested the challenge endpoint with a persistent HTTP session.
- Loaded the events API in headless Chromium with JavaScript enabled and an extended virtual-time budget.

All HTML and API routes remained behind the same robot challenge. No category, genre, organizer, series, or tag values could therefore be enumerated or tested across pagination. Search-engine evidence shows concrete Spokane Symphony performances still exist, so this is not an empty-calendar case.

## What would unblock implementation

Allowlisting the crawler's production egress IP, disabling the robot challenge for the public event pages and read-only `/wp-json/tribe/events/v1/` endpoints, or providing another first-party structured feed (such as JSON, iCalendar, or RSS) would allow the event catalogue, archives, filters, pagination, and detail fields to be validated and a crawler to be implemented.
