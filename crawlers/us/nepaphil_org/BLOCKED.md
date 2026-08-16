<!-- crawler-factory-metadata
{"url":"https://nepaphil.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-16","retry_after":"2026-09-15"}
-->

# NEPA Philharmonic crawler blocked

## Original URL

https://nepaphil.org/

## Why implementation is blocked

The canonical site is the US-based NEPA Philharmonic, and indexed first-party
content shows that it publishes concrete orchestral and chamber events. However,
every tested site route is currently protected by an interactive SiteGround
CAPTCHA. Requests receive a `202` HTML challenge response rather than event data.
A production HTTP crawler would therefore return no records and could not
reliably distinguish an empty calendar from blocked access.

## Approaches attempted

- Opened the home page with Playwright and inspected all network requests. The
  browser was redirected to `/.well-known/sgcaptcha/` and then an interactive
  `/.well-known/captcha/` page. No application API or event-feed requests were
  made before the challenge.
- Waited on the challenge and inspected its network traffic. It remained on the
  CAPTCHA page; the only dynamic endpoint was the CAPTCHA image endpoint.
- Requested the home page, `/events/`, `/concerts/`, `/robots.txt`, and
  `/sitemap.xml` directly with multiple user agents. Every route returned the
  same `202` challenge shell.
- Tested likely WordPress and The Events Calendar interfaces:
  `/wp-json/`, `/wp-json/wp/v2/types`,
  `/wp-json/tribe/events/v1/events`,
  `/?rest_route=/tribe/events/v1/events`, `/events/?ical=1`, and
  `/?post_type=tribe_events&eventDisplay=past`. All were CAPTCHA-blocked.
- Confirmed through indexed first-party pages that `/events/` contains concrete
  dated performances and archives, but search-engine excerpts are not a stable,
  complete, first-party scraping interface.

## Filters and upload-target assessment

No applicable first-party genre, category, discipline, event-type, series, or
tag filter could be inspected or tested because the event calendar and APIs are
blocked before application content loads. Pagination and date-range persistence
could therefore not be verified. The organization presents orchestral, chamber,
pops, film-with-live-orchestra, family, and crossover programming that is broadly
within project scope, while at least one indexed event (a standalone Battle of
the Bands) shows that organization ownership alone is not sufficient evidence
for direct classical upload. If access is restored and no comprehensive stable
filter exists, the appropriate upload target is `potential`; a verified feed of
only concrete NEPA Philharmonic performances may qualify for `classical`.

## What would unblock implementation

Any stable machine-readable first-party route that does not require solving an
interactive CAPTCHA would unblock the crawler, such as allowlisting the crawler
host, exposing the WordPress/Tribe REST or iCal feed, or providing an equivalent
documented event endpoint. The calendar and relevant adjacent categories must
then be checked across pagination and past/future date ranges before selecting
the production feed and upload target.
