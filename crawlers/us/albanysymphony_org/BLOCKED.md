<!-- crawler-factory-metadata
{"url":"https://www.albanysymphony.org/","geographic_scope":"country","country_code":"US","reason_code":"no_current_events","attempted_at":"2026-08-15","retry_after":"2026-09-14"}
-->

# No scrapeable concerts currently published

Original URL: https://www.albanysymphony.org/

The source is the Albany Symphony Orchestra in Albany, Georgia, United States. Its first-party **CONCERTS** link points to `https://www.albanysymphony.org/2023/24-season`, but that page currently contains only “2026 - 2027 SEASON INFORMATION COMING SOON!” It publishes no concert title, date, venue, or detail link. The site's current XML sitemap exposes no concert detail pages or other season archive pages, so there are no current or past concert records that can satisfy the crawler's required fields.

## Investigation performed

- Loaded the home page and concert page with Playwright and inspected their network requests first. No concert API, GraphQL endpoint, JSON event feed, or structured event request was made; the pages are served as static site content.
- Inspected the rendered concert-page HTML/text and links. There are no event cards, event structured data, pagination controls, date ranges, genres, categories, disciplines, event types, series, tags, or other first-party filters.
- Inspected `https://www.albanysymphony.org/sitemap.xml`. Its only concert/season URL is `/2023/24-season`, now used as the empty 2026–2027 placeholder. No individual concert or historical season pages are listed.
- Checked the adjacent `/upcoming-fundraisers` and `/education` pages; both contain no dated events. Checked `/symphony-a-la-carte`; it is an inquiry form for privately booking musicians, not a concrete public performance.
- Because the source is an orchestra's own concert calendar, concrete concerts would normally be in project scope and suitable for the classical upload target. There is presently no feed to select and no filter or pagination behavior to test.

## What would unblock implementation

Publication of the 2026–2027 concert schedule (or restoration of scrapeable archive/detail pages) with concrete dates and defensible venues/cities would allow an HTML crawler to be implemented. A newly exposed structured event API or first-party calendar feed would also unblock implementation and should be preferred if one appears.
