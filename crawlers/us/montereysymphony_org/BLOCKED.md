<!-- crawler-factory-metadata
{"url":"https://www.montereysymphony.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-19","retry_after":"2026-09-18"}
-->

# Monterey Symphony crawler blocked

Original URL: https://www.montereysymphony.org/

The source is the US-based Monterey Symphony and publishes concrete classical
concerts in Carmel, California. A crawler cannot currently be implemented
because the first-party site returns HTTP 406 with an empty response body to
both browser and direct HTTP clients. The block applies to listing pages,
individual event pages, and likely discovery/API endpoints, so there is no
first-party response that a production parser can consume or validate.

## Approaches attempted

- Loaded the canonical homepage with Playwright, both with and without `www`.
  The non-`www` URL redirects to `www`; the final response is HTTP 406.
- Tested the current-season listing (`/current-season/`), its legacy `.htm`
  variant, and a representative current event detail page. Querying with
  `PageSpeed=noscript` did not bypass the HTTP 406 response.
- Inspected Playwright network traffic. It contains only the initial redirect
  and blocked document request; no calendar XHR, JSON, GraphQL, or other event
  API request is allowed to start.
- Probed likely discovery/API routes (`/robots.txt`, `/sitemap.xml`, `/api/`,
  `/wp-json/`, `/wp-json/wp/v2/events`, and a JSON-format season URL). These
  also return HTTP 406, and no usable first-party API was exposed.
- Repeated the request with a direct HTTP client and browser-style headers.
  The site still returned HTTP 406 with a zero-byte body.
- Confirmed through current search-engine indexing that the source has a
  2026-2027 season, concrete event detail pages, performance dates, venues,
  programme descriptions, and older 2025-2026 event pages. Search snippets are
  incomplete third-party representations and are not a stable or sufficiently
  comprehensive source for a production crawler.

The site appears classical-only: the indexed season consists of performances
presented by the Monterey Symphony, including symphonic repertoire and
orchestral crossover concerts within project scope. No first-party category,
genre, discipline, event-type, series, or tag filter could be inspected because
every applicable first-party page and API candidate is blocked. Pagination and
filter persistence therefore could not be tested.

## What would unblock implementation

Any of the following would allow a crawler to be built and validated:

- removal or adjustment of the site's HTTP 406 bot/network rule for ordinary
  read-only requests;
- a documented or accessible first-party calendar/API endpoint;
- an allowlisted production crawler egress address or required non-secret
  request convention; or
- first-party HTML/JSON exports supplied by the organization that cover both
  the current season and published archives.

Once access is restored, investigation should begin with the current-season
page's browser network requests, verify archive coverage, and then fall back to
HTML parsing of season and event-detail pages if no structured API exists.
