<!-- crawler-factory-metadata
{"url":"https://lorenzopalomo.com/","geographic_scope":"country","country_code":"ES","reason_code":"no_current_events","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# Crawler blocked: no scrapeable concert listings

## Original URL

https://lorenzopalomo.com/

The source is the official website of Spanish composer Lorenzo Palomo, so the
resolved source country is Spain (`ES`). The site describes performances of his
music in many countries, but it is not a multi-country event calendar.

## Why a crawler cannot currently be implemented

The website does not publish a calendar, news feed, or archive of concrete
concert occurrences. Its available material consists of static biographical,
composition, discography, gallery, review, and career-history pages. The
publication dates attached to WordPress pages and composition projects are CMS
dates, not performance dates. No records can therefore be produced with a
defensible event date, venue, and city.

## Investigation performed

- The canonical HTTPS URL was tested in Playwright but failed TLS negotiation
  with `ERR_SSL_VERSION_OR_CIPHER_MISMATCH`. The HTTP version is reachable and
  renders the official website.
- Browser network requests were inspected first. No event request or calendar
  API was loaded, but the site's first-party WordPress REST API was discovered
  at `http://lorenzopalomo.com/wp-json/`.
- The REST post types and their full collections were inspected. The API exposes
  0 posts, 9 static pages, and 45 `project` entries in a single page of results.
  The projects are compositions and recordings, not concert occurrences.
- The REST search endpoint was tested with `concert`, `festival`, `premiere`,
  and the years 2023 through 2026. Results only pointed to static résumé,
  composition, gallery, or review content; there were no dated event records.
- First-party taxonomies were inspected. Ordinary post categories (`Blog`,
  `Thoughts`, `Uncategorized`, and `Web`) all contain zero posts. Project
  categories such as `Composition`, `Ballet`, and `Discography` classify works
  or recordings rather than performances. There is no event genre, discipline,
  series, tag, or event-type filter to test across pagination or date ranges.
- Representative rendered HTML was checked, including the “Orchestras & Concert
  Halls” page. It is an undated list of orchestras and venues that have presented
  the composer's music, not an archive of concrete performances.
- The English navigation and the Spanish and German site variants expose the
  same content structure and no concert-calendar section.

## What would unblock implementation

A first-party calendar, news/archive section, or API feed containing concrete
performances with real dates and identifiable venues and cities would make a
crawler possible. Repairing HTTPS alone would not unblock the crawler because
the reachable HTTP site currently contains no scrapeable event occurrences.
