<!-- crawler-factory-metadata
{"url":"https://www.ssorchestra.org/","geographic_scope":"country","country_code":"US","reason_code":"no_current_events","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# Crawler blocked: no published events

The original URL is https://www.ssorchestra.org/. It is the website of the
Susquehanna Symphony Orchestra in Bel Air, Maryland, United States.

The source currently exposes no scrapeable concerts. Its first-party
`/event-list` page uses Wix Events but renders “No events at the moment,” while
the first-party `/this-season` page says to stay tuned for the 2026–2027
schedule. The current Wix site does not expose past events or a concert archive.

## Approaches attempted

- Inspected the home, season, event-list, event-detail, events, and schedule
  routes and the site's Wix configuration.
- Captured the event-list page's browser network traffic. Wix loaded the Events
  widget and its empty-state code but made no event query containing records,
  pagination tokens, date ranges, categories, tags, or other usable filters.
- Inspected the rendered HTML. The Events widget reports zero events, and the
  season page contains no concert cards, dates, venues, or detail links.
- Checked the site's sitemap, robots file, legacy WordPress API and sitemap
  routes, and search-visible legacy pages. The migrated Wix site returns no
  usable sitemap or WordPress archive; old search results do not form a stable,
  complete first-party concert feed.
- Inspected the linked ticket-organizer page as a possible fallback, but it is
  an external sales platform rather than a complete first-party archive and
  does not provide a stable comprehensive feed suitable for this crawler.

The organization itself is a classical orchestra, so a future working feed
would be eligible for `upload_target="classical"`; no genre/category filter is
needed or currently exposed. There are consequently no filter values or
pagination behavior to validate at this time.

## What would unblock implementation

Publication of the 2026–2027 schedule as concrete event records on the Wix
Events page (or another stable first-party calendar/API), with valid dates,
venues, cities, and detail pages, would unblock implementation. The source
should be retried after the new season has been published.
