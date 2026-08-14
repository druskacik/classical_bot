<!-- crawler-factory-metadata
{"url":"https://www.manilasymphony.com/","geographic_scope":"country","country_code":"PH","reason_code":"access_blocked","attempted_at":"2026-08-14","retry_after":"2026-09-13"}
-->

# Crawler blocked by robot challenge

The requested source is the Manila Symphony Orchestra website at
https://www.manilasymphony.com/. It is a Philippines-based classical orchestra,
so the resolved geography is country scope with country code `PH`.

The site still appears to publish concerts: a recent search-engine index of the
homepage contains both "UPCOMING EVENTS" and "PAST EVENTS" sections. However,
all direct requests from the crawler environment return an HTTP 202 SiteGround
robot-challenge bootstrap page instead of the requested content. In Playwright,
the homepage redirects to `/.well-known/sgcaptcha/`; its network log contains
only the challenge assets and exposes no event API or page data.

The following first-party API and discovery approaches were attempted:

- homepage navigation with Playwright and inspection of its network requests;
- `robots.txt`, `sitemap.xml`, and `wp-sitemap.xml` discovery endpoints;
- WordPress REST API forms at `/wp-json/`, `/wp-json/wp/v2/pages`, and
  `/?rest_route=/wp/v2/pages`;
- the WordPress `/feed/` endpoint;
- likely HTML listing paths `/events/`, `/concerts/`, and `/shows/`;
- HTTPS and HTTP requests with and without the `www` hostname.

Every first-party route was intercepted before API or HTML content could be
inspected. No genre, category, discipline, event-type, series, or tag filters
could therefore be tested, and pagination or date-range persistence could not
be evaluated. Search-indexed HTML was also insufficient: event cards are exposed
as images and ticket links without the required event-level date, venue, city,
description, or stable detail URL. It cannot serve as a universal parser source.

Implementation can resume when the SiteGround challenge permits automated
read-only access from the crawler environment, or when the organization exposes
an accessible first-party event feed/API containing concrete event dates,
venues, cities, and URLs. At that point, network inspection should be repeated
before falling back to HTML parsing.
