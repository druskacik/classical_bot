<!-- crawler-factory-metadata
{"url":"https://www.orchestradellatoscana.it/","geographic_scope":"country","country_code":"IT","reason_code":"access_blocked","attempted_at":"2026-08-13","retry_after":"2026-09-12"}
-->

# Access blocked by mandatory CAPTCHA

The original source is https://www.orchestradellatoscana.it/. It is the website
of the Florence-based Orchestra della Toscana, so the resolved geography is
Italy (`IT`), despite individual tour performances in other countries.

The website publishes concrete current and archived concerts, but all tested
first-party routes return HTTP 202 and redirect to a SiteGround Robot Challenge
Screen. The challenge requires solving an image CAPTCHA and setting a browser
cookie. Automating or bypassing that challenge would not be a reliable or
appropriate production-crawler strategy.

## Investigation performed

- Opened the homepage with Playwright and inspected its network requests. The
  browser was redirected before application traffic loaded, so no event API
  request could be reconstructed.
- Tested WordPress REST discovery and REST routes, including `/wp-json/`,
  `/wp-json/wp/v2/`, and `?rest_route=/wp/v2/types`; all were intercepted by the
  same challenge.
- Tested HTML and discovery routes including `/eventi/`, category archives,
  individual event URLs, `robots.txt`, `sitemap_index.xml`, `wp-sitemap.xml`,
  and event/category feeds. These were also intercepted.
- Retried direct HTTP access with the canonical and non-`www` hosts, HTTP and
  HTTPS, ordinary browser headers, JSON accept headers, and a crawler user
  agent. None reached first-party event content.
- Verified through publicly indexed results that `/eventi/` is a paginated
  archive with concrete performances and that detail pages contain date, time,
  city, venue, performers, repertoire, and long descriptions. Indexed category
  routes include exact values such as `stagione-concertistica`,
  `gruppi-da-camera`, `edu-e-famiglie`, `concerti-d-estate`, and year tags.
  However, cached search snippets are incomplete, externally controlled, and
  cannot serve as a universal first-party crawler feed.

No filter could be validated across live pagination or date ranges because the
CAPTCHA blocks every first-party response. The unfiltered archive appears to be
an orchestra calendar rather than a general mixed arts calendar, but its full
coverage and contamination cannot be verified while access is blocked.

## What would unblock implementation

Implementation can proceed when the host permits non-interactive read access to
the event archive and detail pages, or provides a stable allowlisted API/feed
that does not require CAPTCHA completion. With that access, the first step
should be to inspect the archive's date/category form network requests, verify
all scope-relevant category values across pagination and past date ranges, and
then parse each concrete detail page for its full programme and description.
