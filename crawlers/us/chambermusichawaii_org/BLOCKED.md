<!-- crawler-factory-metadata
{"url":"https://www.chambermusichawaii.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-18","retry_after":"2026-09-17"}
-->

# Crawler blocked by SiteGround robot challenge

The original URL is https://www.chambermusichawaii.org/. Chamber Music Hawai‘i
is a United States classical-music organization based in Hawai‘i, so the
resolved crawler geography is US.

A crawler cannot currently be implemented because SiteGround intercepts every
tested first-party page and machine-readable route with an HTTP 202 robot
challenge. The challenge does not resolve in a normal Playwright session, and
direct requests receive a short challenge document rather than event data. A
production crawler would therefore be unable to locate or parse concerts.

Publicly indexed first-party results confirm that the site has current concrete
performances and archives. The site is classical-only: its mission and calendar
describe chamber-music concerts by its resident string, wind, and brass
ensembles. The calendar exposes a first-party category named `Concert Series`
at `/events/category/concert/`; indexed representative events contain dates,
times, venues, programme descriptions, and canonical classical repertoire.
There was no evidence of an applicable adjacent event category that would add
other in-scope public performances. However, access protection prevented live
verification of the category identifier, pagination, past/future date ranges,
and current adjacent-category taxonomy. Search-indexed excerpts are incomplete
and are not a stable source for a universal production crawler.

The following API, network, and HTML approaches were attempted:

- Loaded both the `www` and apex-domain homepages with Playwright and inspected
  network requests. Navigation was redirected to `/.well-known/sgcaptcha/`; the
  only additional requests were challenge assets, so no concert API request was
  exposed for reconstruction.
- Opened the conventional WordPress REST route `/wp-json/wp/v2/types` in
  Playwright. It was intercepted by the same challenge before returning JSON.
- Requested the likely The Events Calendar APIs
  `/wp-json/tribe/events/v1/events`, `/wp-json/wp/v2/tribe_events`, and the
  `?rest_route=/tribe/events/v1/events` equivalent. Every route returned HTTP
  202 challenge HTML.
- Requested the full events listing, the `Concert Series` category listing, a
  past-date list query, and the representative `/concert/twin-horns/` detail
  page. Every HTML source was challenged.
- Requested `robots.txt`, `sitemap_index.xml`, and `wp-sitemap.xml`; these were
  challenged as well and exposed no alternate event feed.
- Checked indexed first-party calendar and detail results. They establish that
  current and archived concerts exist, but do not provide reliable complete
  pagination or a production-safe retrieval mechanism.

Implementation would be unblocked if the publisher exempts a read-only calendar
or REST route from the SiteGround challenge, allowlists the crawler runtime, or
provides an accessible first-party JSON, iCalendar, RSS, or XML feed. Once
access is restored, the The Events Calendar API and the `Concert Series`
category should be tested first across pagination and past/future date ranges.
If that category remains comprehensive and classical-only, the crawler can use
`upload_target="classical"`.
