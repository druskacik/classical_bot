<!-- crawler-factory-metadata
{"url":"https://www.citymusiccleveland.org/","geographic_scope":"country","country_code":"US","reason_code":"no_current_events","attempted_at":"2026-08-18","retry_after":"2026-09-17"}
-->

# CityMusic Cleveland crawler blocked

## Original URL

https://www.citymusiccleveland.org/

## Why a crawler cannot currently be implemented

CityMusic Cleveland is a Cleveland, Ohio chamber orchestra and is therefore a
US-scoped, classical-only source. However, the current website does not publish
any concrete concert occurrences that can be scraped. The homepage says to stay
tuned for the upcoming 2026–27 Orchestra Series, while its Concerts navigation
contains only a locations/venues page. No current or past concert archive is
exposed by the site.

Without a listing or detail page containing a real performance date, title,
city, and venue, a crawler would either return no records or manufacture required
fields. Neither is a working universal crawler.

## Approaches attempted

- Inspected the homepage and its Playwright network traffic. The only relevant
  first-party API calls were Squarespace census/form rendering requests; there
  was no event, calendar, collection, GraphQL, or JSON concert feed.
- Inspected all Concerts navigation links. The only published destination was
  `https://www.citymusiccleveland.org/locations-/-venues`, which contains venue
  profiles rather than concrete performances.
- Inspected the site's Squarespace sitemap for hidden collections, detail pages,
  archives, seasons, series, and concert URLs. It exposes organization, musician,
  guest-artist, sponsor, and venue profile pages, but no scrapeable concert
  occurrences.
- Checked the rendered HTML and page links for dates, event cards, calendar
  widgets, category/tag filters, pagination, and date-range controls. No concert
  feed or applicable first-party filter is currently present.

## What would unblock implementation

Publication of the 2026–27 schedule as concrete event listing/detail pages, or a
first-party calendar/API feed containing event dates, titles, cities, and venues,
would make a crawler implementable. The site should be retried after the upcoming
season is announced.
