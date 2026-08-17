<!-- crawler-factory-metadata
{"url":"https://williamsportsymphony.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Williamsport Symphony Orchestra crawler blocked

Original URL: https://williamsportsymphony.org/

The source is the Williamsport Symphony Orchestra in Pennsylvania, United States, so the resolved country code is `US`. A crawler cannot currently be implemented because Cloudflare returns an HTTP 403 challenge page to normal browser and unauthenticated HTTP access for all concert-bearing routes tested. The only accessible source document was `robots.txt`; it reveals that the site uses WordPress and The Events Calendar, but it contains no concert records.

## Approaches attempted

- Loaded the homepage and `www` hostname with Playwright. Both returned the Cloudflare "Just a moment..." challenge with HTTP 403.
- Inspected Playwright network requests. They contained only the blocked document and Cloudflare challenge/Turnstile traffic; no concert API request or event payload was exposed.
- Tested the likely first-party The Events Calendar REST endpoint at `/wp-json/tribe/events/v1/events?per_page=50` and its WordPress `rest_route` equivalent. Both returned the same HTTP 403 challenge instead of JSON.
- Tested the WordPress REST index and sitemap. Both were blocked with HTTP 403.
- Tested the HTML event calendar at `/events/` and the past-events view at `/events/list/?eventDisplay=past`. Both were blocked with HTTP 403, so current events, archives, pagination, detail pages, venues, and dates could not be inspected.
- Tested the calendar export at `/events/?ical=1`. It was also blocked with HTTP 403.
- Inspected `robots.txt`, which was accessible and explicitly references The Events Calendar query parameters. It does not provide event details, categories, genre filters, or archive content.

No first-party genre, category, discipline, event-type, series, or tag filter could be inspected through the blocked API or HTML. Consequently, filter persistence across pagination and date ranges, coverage, contamination, and representative event details could not be verified. Although the organization is evidently a classical orchestra, there is no scrapeable concert feed from which valid required records can currently be produced.

## What would unblock implementation

Implementation can resume when the site permits non-interactive access to at least one of the Events Calendar REST API, HTML event archive/detail pages, or iCalendar export. An allow-list for the crawler, a stable first-party feed not protected by the challenge, or exported event data with canonical URLs, dates, venues, and cities would also unblock it.
