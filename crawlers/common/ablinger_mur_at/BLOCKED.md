<!-- crawler-factory-metadata
{"url":"https://ablinger.mur.at/","geographic_scope":"multi_country","country_code":null,"reason_code":"no_current_events","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# No scrapeable concert source

## Original URL

https://ablinger.mur.at/

## Why a crawler cannot currently be implemented

The site is Peter Ablinger's static composer archive. It publishes a biography,
a catalogue of works, writings, recordings, and documentation of compositions,
but it does not publish a calendar or archive of concrete concert occurrences.
The years attached to catalogue entries describe composition or publication
years, not performances. Consequently the site supplies no records from which
the required combination of a real event date, venue, and city can be extracted
or defensibly inferred.

The source has no event genre, category, discipline, type, series, or tag
filters. It also has no event pagination or date-range controls. Its scope would
be multi-country if a performance calendar were added, because this is a
composer catalogue rather than the calendar of a venue or home-city institution.

## Approaches attempted

- Loaded the home page and the `akt.html` Documentations page with Playwright
  and inspected all network requests. Both are static HTML pages; no XHR, fetch,
  JSON, GraphQL, or other event API requests were made.
- Inspected the Documentations archive and the `werke.html#recent` recent-works
  section. They contain work pages and composition/publication years, but no
  scrapeable concert listings with occurrence dates and locations.
- Checked for first-party event links and filters using calendar, concert,
  event, news, and date-related terms. The only concert-labelled material is
  documentation about works such as "Concert-Installations", not an event feed.
- Requested `robots.txt`, `sitemap.xml`, `termine.html`, `calendar.html`, and
  `news.html` through Playwright. Each endpoint returned HTTP 404, so they did
  not reveal a hidden calendar, sitemap, API, or archive.

## What would unblock implementation

A first-party calendar or archive containing concrete performances with valid
dates, venues, and cities, or a stable first-party API/feed exposing those
fields, would make a crawler possible. An external presenter or ticketing feed
would be a different source and cannot be reconstructed from this website.
