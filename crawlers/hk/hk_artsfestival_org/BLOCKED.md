<!-- crawler-factory-metadata
{"url":"https://www.hk.artsfestival.org/","geographic_scope":"country","country_code":"HK","reason_code":"no_current_events","attempted_at":"2026-08-12","retry_after":"2026-09-11"}
-->

# Crawler blocked: Hong Kong Arts Festival

The original URL is https://www.hk.artsfestival.org/. It redirects to the
English home page at `https://www.hk.artsfestival.org/en/`; the organization and
its published programme are based in Hong Kong, so the resolved country code is
`HK`.

A production crawler cannot currently return valid concert records because the
site's newly announced 55th-festival programme does not yet publish concrete
performance dates or venues. The programme feed exposes 28 programme pages, but
their structured performance objects have null start times and venue data (and
most pages have no performance objects at all). Returning these programme pages
would create season/programme overviews without the required real occurrence
date and venue.

## Investigation performed

- Used Playwright to inspect the home page, programme listing, representative
  programme details, and their network requests.
- Reconstructed the site's first-party Gatsby JSON interface from
  `/page-data/en/programme/page-data.json` and the per-programme
  `/page-data/en/programme/<permalink>/page-data.json` responses.
- Tested the first-party genre values `music`, `opera`, `dance`, `theatre`,
  `musictheatre`, `chineseopera`, `dancetheatre`, and `artstech`, plus the
  category value `in-venue`. These identifiers are embedded consistently in the
  single, non-paginated 28-item programme response; there is therefore no
  pagination or date-range persistence to validate. They are broad artistic
  disciplines rather than sufficiently precise classical-scope filters.
- Inspected representative music (orchestral, recital, crossover, and folk),
  opera/operetta, ballet/dance, music-theatre, Chinese-opera, theatre, and
  programme mother pages. The listing is a mixed festival feed and includes
  both eligible and nonclassical/uncertain material. A future crawler should
  consequently submit concrete occurrences to the `potential` upload target
  unless the source later adds a comprehensive, stable first-party filter.
- Checked the sitemap and the first-party past-programme pages, including the
  2026 archive. Those archives provide programme titles and external house
  programme links, but no parseable occurrence dates, times, or venues. HTML
  parsing therefore cannot produce valid archived concert records either.

Implementation will be unblocked when the programme detail JSON publishes
non-null performance occurrences containing dates and venues. At that point a
crawler can use the Gatsby listing and detail JSON directly, skip mother pages,
expand each concrete performance into a record, and send the mixed candidate
feed to classification.
