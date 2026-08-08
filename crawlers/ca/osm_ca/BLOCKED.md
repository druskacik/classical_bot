<!-- crawler-factory-metadata
{"url":"https://www.osm.ca/","geographic_scope":"country","country_code":"CA","reason_code":"access_blocked","attempted_at":"2026-08-08","retry_after":"2026-09-07"}
-->

# Crawler blocked

## Original URL

https://www.osm.ca/

## Why implementation is blocked

The OSM website publishes a concert calendar, including current and archived
concert pages, but all public HTML pages return HTTP 403 to automated clients.
This includes both `www.osm.ca` and the canonical `osm.ca` host. Without access
to the calendar HTML or the JavaScript application that loads it, a crawler
cannot discover concert URLs or reliably extract dates, venues, cities, and
programme descriptions.

## Approaches attempted

- Opened the home page and canonical host with Playwright and inspected the
  network requests. Navigation stopped at an HTTP 403 response before scripts
  or concert-data requests could load, so no event API appeared in the network
  log.
- Tested the French programme page and WordPress page-ID variants with browser
  and HTTP clients. They also returned HTTP 403, including requests with normal
  browser and search-crawler user agents.
- Inspected the public WordPress REST API. The calendar page is exposed only as
  an empty page shell using the `tpl-programmation` template. The API exposes no
  concert post type or concert/calendar route.
- Enumerated the site's REST namespaces and tested its custom
  `osm-tnew/v1/session` endpoint. That endpoint returns only login/cart state,
  not performances or event details.
- Checked WordPress search and sitemap options. Search does not return concert
  posts, while the public sitemap endpoints are themselves blocked with HTTP
  403.

## What would unblock implementation

Any of the following would make a reliable crawler possible:

- allowlisted access to the public calendar and concert HTML;
- documentation or a captured browser request for the calendar's structured
  event API; or
- a public feed/sitemap that lists concert detail URLs and remains accessible
  to the production crawler.

The resolved source geography is Canada (`CA`): OSM is a Canadian orchestra.
Its occasional international touring dates do not make the source itself a
multi-country publisher.
