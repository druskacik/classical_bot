<!-- crawler-factory-metadata
{"url":"https://www.accademiabizantina.it/","geographic_scope":"country","country_code":"IT","reason_code":"access_blocked","attempted_at":"2026-08-12","retry_after":"2026-09-11"}
-->

# Accademia Bizantina crawler blocked

Original URL: https://www.accademiabizantina.it/

Accademia Bizantina is an Italian classical-music ensemble whose first-party
event calendar includes concrete performances in Italy and on tour. A crawler
cannot currently be implemented because SiteGround's anti-bot layer intercepts
all tested requests. It returns HTTP 202 with only a meta-refresh to
`/.well-known/sgcaptcha/`; the resulting page is a "Robot Challenge Screen".
There is no event HTML or structured payload available to the production
`requests` client.

## Approaches attempted

- Loaded the canonical home page with Playwright and inspected its network
  requests. The browser was redirected to the SiteGround CAPTCHA, and the only
  subsequent requests were challenge assets; no calendar API was exposed.
- Requested the canonical HTTP and HTTPS hosts, with and without `www`, using a
  browser-like user agent. Each returned the same 202 CAPTCHA shim.
- Probed the first-party event routes `/eventi/` and `/prossimi-eventi/`, an
  indexed event-detail route, and WordPress REST discovery at
  `/wp-json/wp/v2/types`. All were intercepted before returning parseable HTML
  or JSON.
- Checked indexed first-party results to confirm that event listings and event
  archives exist. They show dates, cities, venues, and classical programmes,
  but a search-engine index is neither a stable first-party feed nor a suitable
  production scraping interface.

The site exposes no usable genre/category filter through the accessible
responses. Indexed evidence indicates that the event calendar belongs to a
classical ensemble and contains its classical concerts (including touring
performances), but filter and pagination behavior could not be verified against
the live source because of the challenge.

Implementation can resume when the site allows non-interactive server requests,
provides a stable first-party API/feed that is not challenge-protected, or
allowlists the crawler's production egress. At that point the calendar and its
archives should be re-investigated, including pagination and detail pages,
before selecting the feed.
