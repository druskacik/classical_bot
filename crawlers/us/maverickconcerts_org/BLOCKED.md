<!-- crawler-factory-metadata
{"url":"https://maverickconcerts.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-16","retry_after":"2026-09-15"}
-->

# Maverick Concerts crawler blocked

## Original URL

https://maverickconcerts.org/

## Why implementation is blocked

Maverick Concerts is a mixed-source, US-based concert presenter in Woodstock,
New York. Its public calendar and first-party WordPress Events Calendar REST API
are protected by a SiteGround robot challenge. Ordinary HTTP clients receive an
HTTP 202 HTML meta-refresh to `/.well-known/sgcaptcha/` instead of either event
HTML or JSON. The repository's production crawler interface uses an HTTP parser;
it cannot safely or reliably complete this browser challenge.

Playwright could render the site after the challenge and confirmed that concerts
are currently published, so this is not an empty-calendar case. However, a
production `requests`-based `scrape()` could not retrieve the source, and a
crawler that depends on challenge/session material obtained interactively would
not be universal or operationally reliable.

## Approaches attempted

- Loaded the home page and event calendar with Playwright. The initial request
  was redirected to SiteGround's Robot Challenge Screen; a later browser
  navigation rendered the calendar.
- Inspected browser network behavior and identified The Events Calendar API at
  `https://maverickconcerts.org/wp-json/tribe/events/v1/events`.
- Confirmed the API exposes structured titles, canonical event URLs, start/end
  datetimes, venues, cities, categories, and long HTML descriptions.
- Confirmed archives are present: an API query spanning 2017 through 2027
  reported 124 events over 42 pages at three records per page, with a stable
  `next_rest_url` carrying `page`, `per_page`, `start_date`, `end_date`, and
  `status=publish`.
- Tested the API types/search routes, REST route variants, sitemap, robots file,
  `www` hostname, HTTP-to-HTTPS route, and event/category HTML with an ordinary
  HTTP client. All non-browser variants returned the same HTTP 202 challenge
  rather than parseable content.

## Feed and filter findings

The source is mixed rather than classical-only. The first-party category API
exposed these relevant exact values among its categories:

- `maverick-chamber-music-festival` (Maverick Chamber Music Festival)
- `maverick-family-saturdays` (Maverick Family Saturdays)
- `jazz` (Jazz at the Maverick)
- `world-music` (World Music)
- `independent-production` (Independent Production)
- `special-events` (Special Events)
- `benefit` (Benefit)
- `past` (past)

Representative calendar and detail content showed that the chamber-music
category is reliably in scope, while adjacent Family Saturdays and Independent
Production categories contain both eligible classical performances and
ambiguous or nonclassical events. Other night-series programming spans classical,
contemporary, jazz, Americana, folk, and world music. Therefore the narrow
chamber category would omit eligible family, choral, contemporary-art-music,
Indian-classical, and independent performances. If access becomes available,
the appropriate selected feed is the full concrete event API across its
available date range with `upload_target="potential"`; first-party categories
should be retained as evidence but not treated as a complete inclusion filter.

The category identifiers were visible in API responses and category archive
URLs, and the unfiltered API's date and pagination parameters persisted in its
generated `next_rest_url`. A complete category-by-category pagination validation
could not be performed without relying on the challenged browser session.

## What would unblock implementation

Any stable, non-interactive access path would unblock the crawler: allowlisting
the production crawler egress/user agent, disabling the challenge for the
read-only Events Calendar REST endpoints, or providing an official API/feed that
does not require browser challenge state. The crawler can then paginate the full
REST event collection, parse its structured fields, default missing locations to
Maverick Concert Hall in Woodstock only where the first-party event evidence
supports that inference, and send the mixed candidate feed to potential-event
classification.
