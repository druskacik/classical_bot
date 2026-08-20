<!-- crawler-factory-metadata
{"url":"https://askellmasson.com/","geographic_scope":"country","country_code":"IS","reason_code":"access_blocked","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# Crawler blocked

The original URL is <https://askellmasson.com/>. It is the official website of
Icelandic composer Áskell Másson, so the resolved geographic scope is Iceland
(`IS`).

A production crawler cannot currently be implemented because Cloudflare returns
HTTP 403 challenge pages for every content-bearing route tested. The challenge
did not resolve in a real Playwright browser, so neither an event feed nor event
detail HTML can be inspected or parsed reliably. The only accessible first-party
resource was `robots.txt`, containing just a global crawl delay and no sitemap
or feed location.

Investigation attempted:

- Loaded the homepage in Playwright and waited for the Cloudflare challenge to
  resolve; it remained an HTTP 403 page.
- Inspected Playwright network requests. They contained only the document,
  Cloudflare Turnstile/challenge traffic, and a challenge blob; no site API,
  structured event endpoint, or content request was exposed.
- Requested `sitemap.xml`, `wp-json/`, `events`, `concerts`, and `calendar` using
  browser and direct HTTP approaches. All content routes returned the same
  Cloudflare 403 response; only `robots.txt` returned HTTP 200.
- Searched indexed first-party pages for an event or concert archive. The only
  current indexed page found was a contact page, which identifies the composer
  and Reykjavik address but provides no concrete concert occurrences.
- Attempted to query historical URL discovery through the Internet Archive CDX
  service, but that service timed out and yielded no usable archive inventory.

Because no event/category page could be reached, there were no first-party
genre, category, discipline, event-type, series, or tag filters to test. Their
pagination and date-range persistence therefore could not be assessed, and no
feed or upload target could be selected safely. In particular, third-party
search results are not a stable source for the required dates, venues, cities,
descriptions, and canonical event URLs.

Implementation can be retried when the site permits automated read access, the
operator provides an allowlisted API/feed, or an accessible first-party event
archive becomes discoverable. At that point the network trace, filters,
pagination, historical coverage, and representative detail pages should be
re-evaluated before choosing `classical` versus `potential`.
