<!-- crawler-factory-metadata
{"url":"https://www.quartettomilano.it/","geographic_scope":"country","country_code":"IT","reason_code":"access_blocked","attempted_at":"2026-08-13","retry_after":"2026-09-12"}
-->

# Access blocked

Original URL: https://www.quartettomilano.it/

The Società del Quartetto di Milano is an Italian, Milan-based classical-music
presenter with current and archived concert listings. A crawler cannot currently
be implemented because SiteGround's robot challenge intercepts requests before
the site returns calendar, archive, event-detail, or machine-readable content.
The challenge returns HTTP 202 and redirects the browser to
`/.well-known/sgcaptcha/`; it cannot be treated as an event response or bypassed
by a production `requests` crawler.

## Approaches attempted

- Loaded the canonical home page with Playwright and inspected its network
  requests. The only first-party navigation was the initial request followed by
  the SiteGround captcha; no calendar or event API request was exposed.
- Tested the non-`www` and HTTP variants. They returned the same challenge.
- Tested likely WordPress interfaces: `/wp-json/`, `/wp-json/wp/v2/types`, and
  `/feed/`. Each was intercepted before structured data was returned.
- Tested discovery endpoints including `/robots.txt`, `/sitemap_index.xml`, and
  `/wp-sitemap.xml`. These were also challenged (the last endpoint did not yield
  usable content within the request window).
- Investigated indexed first-party URLs. Search results confirm concrete current
  events under `/event-item/...`, a calendar at `/calendario-eventi/`, and an
  archive at `/archivio-dei-concerti/`, but search-engine excerpts are incomplete,
  stale, and not a stable first-party feed suitable for a universal crawler.

## Filters and source scope

No first-party genre, category, discipline, event-type, series, or tag filter
could be tested because all live first-party endpoints were intercepted. Indexed
copies show location/series archive pages and event categories, but their exact
stable identifiers, pagination behavior, and date-range coverage could not be
verified. The organization appears classical-only, so an accessible complete
calendar/archive feed would likely qualify for direct classical upload after
representative detail checks.

## What would unblock implementation

Any stable first-party endpoint that is accessible to the production crawler,
such as an allowlisted calendar/archive HTML route, WordPress REST endpoint,
XML feed, or documented event API. Access must include pagination or archive
discovery plus event detail pages so dates, times, venues, cities, and programme
descriptions can be extracted and verified.
