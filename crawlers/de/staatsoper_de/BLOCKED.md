<!-- crawler-factory-metadata
{"url":"https://www.staatsoper.de/","geographic_scope":"country","country_code":"DE","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Bayerische Staatsoper crawler blocked

## Original URL

https://www.staatsoper.de/

The source is the Bayerische Staatsoper in Munich, Germany, so the resolved country code is `DE`.

## Why implementation is currently blocked

The site returns HTTP 403 for the home page, dated schedule pages, archive pages, the sitemap, and production-detail access. The response is a generic Bayerische Staatsoper block page rather than the requested calendar content. This prevents a crawler from reliably discovering event URLs, following pagination or month ranges, extracting the long production descriptions, and validating required date/venue/city fields.

Search-engine-indexed first-party pages show that concerts are published and that the archive extends back to 2001, so this is not an empty-calendar case. However, indexed snippets are not a stable or sufficiently complete source for a production crawler.

## Approaches attempted

- Loaded the canonical home page and a dated schedule page with Playwright and inspected their network traffic. Both returned HTTP 403 before application requests ran; only the document request and Cloudflare telemetry were exposed, so no schedule API could be reconstructed.
- Tried the canonical `www` host, the bare host and its redirect, German and English dated schedule routes, archive routes, the sitemap, query-string variants, browser and crawler user agents, and normal browser request headers. Calendar-related routes consistently returned HTTP 403. Only `robots.txt` was accessible.
- Inspected the 403 HTML for API, AJAX, calendar, structured-data, and JavaScript application endpoints. It contained no event data or usable application endpoint.
- Checked indexed first-party schedule, archive, production, subscription, and festival pages. They demonstrate concrete Opera, Ballett, Konzert, Kind&Co, and Extra entries, including venue and performance time, but do not provide a complete, durable replacement feed or verified detail-page bodies.

## Filters and feed assessment

The first-party calendar visibly exposes event-type/category choices including `Oper`, `Ballett`, `Konzert`, `Kind&Co`, and `Extra`, plus flags including `Premiere`, `Staatsoper TV`, `<30`, `Familienvorstellung`, `Oper für alle`, `Opernfestspiele`, and `Ballettfestwoche`. Indexed representative results show qualifying opera, ballet, and concert performances, while `Extra` is contaminated by tours and other non-concert listings. No stable query values or API identifiers could be tested because every interactive calendar request was blocked before the application loaded. Their persistence across pagination and date ranges therefore could not be verified, and no feed or upload target can responsibly be selected yet.

## What would unblock implementation

Any of the following would permit a reliable crawler:

- allowlisting the crawler runtime or removing the current 403 response for public schedule and detail pages;
- documentation or access details for the first-party schedule API/feed; or
- saved, representative successful browser network responses for the calendar, pagination/month navigation, category filtering, and production details.

Once access is restored, investigation should verify the category identifiers across month and archive navigation, combine all qualifying opera/ballet/concert/family categories, inspect adjacent `Extra` records for contamination, and then choose `classical` only if the resulting first-party feed is comprehensively in scope; otherwise it should use `potential`.
