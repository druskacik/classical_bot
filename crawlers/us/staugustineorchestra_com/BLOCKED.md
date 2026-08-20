<!-- crawler-factory-metadata
{"url":"https://www.staugustineorchestra.com/","geographic_scope":"country","country_code":"US","reason_code":"no_parseable_source","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# Crawler blocked: concert data is image-only

## Original URL

https://www.staugustineorchestra.com/

## Why a crawler cannot currently be implemented

The St. Augustine Orchestra is an active, US-based classical-only source, and its ticket page currently advertises a 2026–2027 season. However, the event title, date, time, venue, location, and programme information are published only as pixels in flyer images. The accessible page text and HTML do not contain those required values. A reliable crawler cannot turn those images into valid records using the repository's current runtime dependencies, and hard-coding the currently visible season would not create a universal scraper that survives site updates.

## Approaches attempted

- Inspected the site with Playwright, including network requests on the home page, ticket page, and both linked concert-detail pages.
- Checked Wix API, page-data, and application requests for a structured event collection or calendar feed. No concert/event API was exposed; the relevant content is delivered as Wix image assets.
- Inspected the ticket-page DOM, accessible text, links, image metadata, and rendered HTML. The page contains a single season-schedule image; its alt text is only `Season schedule for 2026-2027 final.jpg`.
- Inspected the linked `Fall 2026 Kelly Farm` and `Fall 2026 The Gilded Table` detail pages. Their bodies contain purchase-ticket controls but no dates, venues, or programme text. Those details are again embedded only in images with generic filename alt text.
- Checked the raw server-rendered HTML for representative visible dates and programme details. They are absent.
- Looked for past concert/archive pages and pagination in the site's navigation. No scrapeable archive or paginated event feed is exposed.
- Checked for first-party genre, category, discipline, event-type, series, and tag filters. None are exposed. Filtering is unnecessary for source classification because the organization and advertised performances are orchestra concerts, but the records remain unparseable.

## What would unblock implementation

Any stable first-party HTML, JSON/API, calendar/iCalendar, structured-data, or ticketing feed containing per-performance title, date, time, venue, and city would unblock the crawler. Alternatively, adding a supported and production-available OCR service or dependency with adequate validation for the flyer images could make extraction possible.
