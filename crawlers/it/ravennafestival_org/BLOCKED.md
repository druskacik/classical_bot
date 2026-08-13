<!-- crawler-factory-metadata
{"url":"https://www.ravennafestival.org/","geographic_scope":"country","country_code":"IT","reason_code":"access_blocked","attempted_at":"2026-08-13","retry_after":"2026-09-12"}
-->

# Ravenna Festival crawler blocked

## Original URL

https://www.ravennafestival.org/

## Why implementation is currently blocked

The canonical website returns HTTP 403 from Amazon CloudFront to both a real
browser session and ordinary HTTP clients. The denial happens before any event
HTML or application data is returned, so there is no stable source that a
production crawler can parse.

Ravenna Festival is based in Italy and its programme is a mixed cultural feed:
it includes classical concerts and opera, but also theatre, contemporary dance,
jazz, folk, and other events. Consequently, an accessible implementation would
need either comprehensive first-party scope filters or the unfiltered candidate
feed with `upload_target="potential"`.

## Investigation performed

- Opened the canonical homepage and the bare-domain redirect with Playwright;
  both ended at `https://www.ravennafestival.org/` with HTTP 403.
- Inspected browser network traffic. Only the blocked document request was
  available; no XHR, Fetch, GraphQL, or other event API request could be
  reconstructed.
- Tested likely first-party WordPress/API and discovery endpoints:
  `/wp-json/`, `/wp-json/wp/v2/types`, `/robots.txt`, `/sitemap_index.xml`, and
  `/wp-sitemap.xml`. Every endpoint returned the same CloudFront 403.
- Tested the likely programme HTML path `/programma/` with an HTTP client; it
  also returned the same 403 response.
- Inspected search-indexed first-party pages, including the archive query form
  `/en/events/?qy=2016` and representative event pages. They show that event
  detail pages historically exposed dates, times, venues, addresses, and long
  programme descriptions, but cached search text is not a complete or reliable
  scrape source.
- Opened `https://old.ravennafestival.org/` with Playwright. It is reachable but
  is a frozen Ravenna Festival 2016 site. Its visible first-party navigation
  filters are `Opera`, `Concerti`, `Altri eventi`, `Teatro & Danza`, `Liturgie
  domenicali`, and `Le vie dell'amicizia`. These values are embedded in stale
  2016 HTML rather than a current paginated feed. They were not usable for
  pagination/date-range persistence testing, and using only `Opera` and
  `Concerti` would omit potentially eligible dance, sacred, crossover, and
  mixed-category performances.

No applicable current first-party genre, category, discipline, event-type,
series, or tag filter could be tested because the current source is inaccessible.
No feed was selected. The old 2016 site is not an acceptable replacement for
the canonical site's current and complete archive.

## What would unblock implementation

Any of the following would permit a retry:

- CloudFront access for the crawler environment to the canonical website;
- a documented or discoverable first-party event API that is accessible from
  the crawler environment; or
- a complete first-party export/feed containing the current programme and
  published archives, including event detail URLs, dates, venues, and cities.

Once access is restored, network requests should be inspected again before
falling back to HTML parsing. Because the source is mixed, its filters must be
checked across pagination and date ranges for all categories allowed by the
project guidance; absent a comprehensive stable filtered feed, the crawler
should scrape the candidate programme with `upload_target="potential"`.
