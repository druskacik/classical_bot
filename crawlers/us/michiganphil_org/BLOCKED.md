<!-- crawler-factory-metadata
{"url":"https://www.michiganphil.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-19","retry_after":"2026-09-18"}
-->

# Michigan Philharmonic crawler blocked

## Original URL

https://www.michiganphil.org/

## Why a crawler cannot currently be implemented

Michigan Philharmonic is a US orchestra based in southeast Michigan, so the
resolved geography is country scope with country code `US`.

The site is protected by a SiteGround robot challenge. Every request made with
the repository's available HTTP stack receives HTTP 202 challenge HTML instead
of the requested page or JSON. This also affects the otherwise suitable
first-party WordPress REST API. A production `BaseCrawler` implementation using
`requests` therefore could not retrieve records, and the project has no browser
or challenge-solving runtime dependency that can safely be used by this
crawler.

## Investigation performed

- Opened the homepage with Playwright. It initially redirected to
  `/.well-known/sgcaptcha/`; the browser eventually completed the JavaScript
  challenge and exposed the site.
- Inspected browser network traffic. The event cards are rendered in the page;
  no separate calendar or ticketing event API request was used by the homepage.
- Inspected the WordPress REST type registry at
  `/wp-json/wp/v2/types`. It exposes a first-party custom post type named
  `concert-event`, with collection endpoint
  `/wp-json/wp/v2/concert-event`.
- Queried `concert-event?per_page=100&page=1` in Playwright. It returned 12
  posts in a single page: nine concrete 2026-2027 performances and three season
  subscription/series overview records. The endpoint exposes no taxonomies or
  genre/category/tag filters for this post type. The source itself is a
  classical orchestra, and representative concrete posts contained orchestral,
  chamber, family, holiday, and classical-crossover programming within the
  project's inclusion guidance. The three subscription posts would need to be
  excluded as non-event overviews.
- Inspected a representative detail page. Its first-party HTML provides a real
  date and time, venue, street address and Michigan city in structured Divi text
  blocks. The REST `content.rendered` field provides the long description and
  full programme, including composers and works.
- Tried the REST API, `?rest_route=` alternative, homepage, `robots.txt`, and
  `sitemap.xml` using direct HTTP requests with browser-like headers. Also tried
  HTTP and HTTPS and both `michiganphil.org` and `www.michiganphil.org`. Every
  route returned the same HTTP 202 robot-challenge document rather than source
  content.
- Tried the public WordPress.com API proxy, but the independently hosted site is
  not registered there and the proxy returned `invalid_site`.

No first-party genre, category, discipline, event-type, series, or tag filter is
available on the custom post type. Pagination itself is stable WordPress REST
pagination (`page` and `per_page`), and all 12 currently exposed posts fit on
page 1, but direct-client access could not be validated across pages or date
ranges because the challenge blocks the production HTTP stack.

## What would unblock implementation

Any one of the following would allow a crawler to be implemented and tested:

- allowlisting the production crawler and crawler-factory IPs in SiteGround;
- disabling the robot challenge for the read-only WordPress REST paths;
- providing another stable first-party feed that is not challenge-protected;
- adding an approved, maintained browser runtime to the production crawler
  image and repository interfaces.

Once access is available, the preferred feed is the complete
`wp/v2/concert-event` collection, excluding subscription/season overview posts
and parsing each concrete detail page. Because this is a classical-orchestra
source and representative concrete posts are in scope, the intended upload
target would be `classical`.
