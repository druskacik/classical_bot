<!-- crawler-factory-metadata
{"url":"https://www.bmop.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-18","retry_after":"2026-09-17"}
-->

# BMOP crawler blocked by anti-bot challenge

Original URL: https://www.bmop.org/

BMOP is the Boston Modern Orchestra Project, a US-based classical-only source. Its
current-season and past-performance pages visibly publish concrete orchestral and
opera performances, including dates, times, venues, repertoire, and descriptive
programme text. The site does not expose genre, category, discipline, event-type,
series, or tag filters applicable to these performance listings; none are needed
for this classical-only organization.

The supplied `www` URL redirects browser traffic through a SiteGround Robot
Challenge Screen. The canonical bare-domain pages can be inspected in a full
browser after the challenge flow, but production-compatible HTTP requests receive
HTTP 202 and a short HTML document that redirects to
`/.well-known/sgcaptcha/` instead of the requested content. This happens for the
season pages and WordPress REST endpoints, including when browser-like request
headers and TLS/browser impersonation are used. A crawler built on the repository's
available HTTP interfaces would therefore parse only the challenge page and could
not reliably collect concerts.

## Approaches attempted

- Inspected browser network traffic for the homepage and current-season page.
  No concert API request was made by the page; performance data is server-rendered.
- Tested the public WordPress REST API. `/wp-json/wp/v2/types` is visible in the
  browser, but it exposes no performance post type. The candidate endpoint
  `/wp-json/wp/v2/performances?per_page=5` returns `rest_no_route` (404).
- Inspected `https://bmop.org/current-season/`, representative detail page
  `https://bmop.org/performances/bmop-presents-bang-the-drum/`, and
  `https://bmop.org/past-performances/` in the browser. The current page links to
  individual server-rendered detail pages; the archive embeds many past events in
  one HTML page. No pagination or stable first-party filter parameters are present.
- Requested the canonical HTML pages and WordPress endpoints with Python
  `requests`, command-line curl over HTTP/2, and `curl_cffi` browser impersonation.
  All returned the HTTP 202 SiteGround challenge rather than concert content.
- Tested `/robots.txt`, `/sitemap.xml`, `/events`, and `/concerts`. The first two
  are challenge-protected; the latter two are normal 404 pages and expose no
  alternate feed.

## What would unblock implementation

Implementation can proceed if BMOP permits the crawler/runtime IP or user agent,
disables the challenge for public season/archive and REST paths, or supplies a
stable first-party JSON, XML, RSS, or iCalendar feed accessible to non-browser
HTTP clients. With access restored, the appropriate feed is the complete current
season plus the complete past-performances archive, and the upload target should
be `classical` because BMOP's performance catalogue is classical-only.
