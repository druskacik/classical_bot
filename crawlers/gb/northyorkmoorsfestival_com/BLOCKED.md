<!-- crawler-factory-metadata
{"url":"https://www.northyorkmoorsfestival.com/","geographic_scope":"country","country_code":"GB","reason_code":"no_parseable_source","attempted_at":"2026-08-12","retry_after":"2026-09-11"}
-->

# Crawler blocked: no parseable concert source

## Original URL

https://www.northyorkmoorsfestival.com/

## Why a crawler cannot currently be implemented

The site publishes concrete North York Moors Chamber Music Festival concerts, but the current programme is a single JPEG image rather than HTML, JSON, or another machine-readable event feed. The image visible on the home page contains the 2026 dates, times, titles, venues, and repertoire, but none of that schedule text is present in the page DOM. There are no individual concert detail pages. A crawler could only reproduce the current image by hard-coding manually transcribed records or by adding an OCR dependency/service; hard-coding would not be a universal crawler and would silently become stale when the programme image changes.

The Past Festivals page links to annual PDF/image brochures (2009 through 2025), not to event records. The repository and production environment do not include a supported PDF or OCR parser, and adding a dependency is outside this crawler-only task. Those brochures therefore do not provide a stable source that this crawler can parse with the repository's current interfaces.

## Approaches attempted

- Loaded the home page and Past Festivals page with Playwright and inspected browser network requests before considering HTML parsing.
- Inspected all non-static network traffic. The only site endpoint observed was Squarespace's `/api/census/RecordHit` analytics request; no events, calendar, GraphQL, JSON, or programme API was called.
- Inspected the page DOM, accessible content, links, images, and JSON-LD. JSON-LD describes only the website. No event objects, dates, venue fields, categories, genres, tags, pagination, or date-range parameters are exposed.
- Checked the first-party current-programme link, `/s/Festival-final-programme.jpg`. It is a raster image containing 12 concrete chamber-music performances from 8–22 August 2026, but it has no embedded text or event-level URLs.
- Inspected the Past Festivals page. It exposes first-party brochure links for 2009–2025, but no HTML event archive or structured feed.
- Checked for applicable first-party filters. The site is classical-only, and it exposes no genre, category, discipline, event-type, series, tag, pagination, or date-range filters to test.

## What would unblock implementation

Any of the following would make a durable crawler possible:

- a first-party HTML calendar or individual event pages;
- a stable JSON/API/iCalendar feed containing the programme;
- a text-based current programme in a format supported by existing project dependencies; or
- explicit approval to add and maintain a production OCR/PDF extraction dependency, together with validation rules for the image and brochure layouts.

If the site later publishes structured records, it should use `upload_target="classical"`: the source is the festival's own chamber-music programme and the representative current and archived material is classical-only.
