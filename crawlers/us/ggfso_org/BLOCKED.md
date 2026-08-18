<!-- crawler-factory-metadata
{"url":"https://www.ggfso.org/","geographic_scope":"country","country_code":"US","reason_code":"no_parseable_source","attempted_at":"2026-08-18","retry_after":"2026-09-17"}
-->

# Crawler blocked: concert data is image-only

The original source is https://www.ggfso.org/, the website of the Greater Grand Forks Symphony Orchestra in Grand Forks, North Dakota, USA.

A reliable universal crawler cannot currently be implemented because the site's concert season is published as a set of raster images. The images contain the titles, dates, times, venues, and repertoire visually, but the page does not expose that information as HTML text, image alternative text, structured data, or linked event-detail pages. The asset identifiers and image contents can change when the season page is edited, so hard-coding the currently visible concerts would not constitute a reusable crawler. Adding runtime OCR would also require an unavailable repository dependency and would not reliably satisfy the required date, city, and venue validation.

## Approaches attempted

- Loaded the home page, `https://www.ggfso.org/currentseason`, and `https://www.ggfso.org/specialevents` with Playwright and inspected their network requests before considering HTML parsing.
- Checked the Wix network traffic for event, calendar, dataset, query, GraphQL, and API requests. Only generic Wix page/bootstrap, access-token, tag-manager, and telemetry requests were present; there was no concert/event API or CMS dataset request to reconstruct.
- Inspected the rendered accessibility tree, document text, links, iframes, images, background images, and image ancestors. The season page exposes only a season heading and generic special-events copy as text. It has no pagination, date-range controls, genres, categories, disciplines, event types, series, or tag filters.
- Downloaded representative original Wix season-image assets under `/tmp` and verified that concrete event details are baked into the pixels. The images are not linked to individual event pages, and most have empty alternative text (one has only a design filename).
- Inspected the adjacent special-events page. It likewise exposes only a heading in HTML and no parseable event feed.

## What would unblock implementation

Implementation would become possible if GGFSO publishes semantic event cards or detail pages, exposes a stable Wix CMS/events API containing the concert fields, adds complete machine-readable text/structured data to the season page, or the project explicitly adopts and supplies a supported OCR dependency plus an acceptable validation strategy for image-only listings.

The organization is a US-based classical orchestra, so the resolved geography remains country scope with country code `US`. If a parseable feed becomes available, this classical-only source would be suitable for the `classical` upload target; no first-party filters would be necessary.
