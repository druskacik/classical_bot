<!-- crawler-factory-metadata
{"url":"https://dzintarukoncertzale.lv/","geographic_scope":"country","country_code":"LV","reason_code":"access_blocked","attempted_at":"2026-08-14","retry_after":"2026-09-13"}
-->

# Crawler blocked by Cloudflare

## Original URL

https://dzintarukoncertzale.lv/

The source is the Latvian venue Dzintaru koncertzāle in Jūrmala, so the resolved
country is Latvia (`LV`). The calendar is mixed rather than classical-only:
search-indexed event pages include classical and orchestral performances, but
also pop concerts, exhibitions, film screenings, and other events.

## Why implementation is currently blocked

Every calendar page, event detail page, sitemap, WordPress REST endpoint, and
calendar-feed endpoint tested returns an HTTP 403 Cloudflare challenge. The
challenge did not resolve in the Playwright browser, and ordinary production-
style HTTP requests receive the same response. A crawler built against the
currently observable response would scrape only the challenge page and could
not reliably enumerate current or archived events.

Search-engine results confirm that concrete current and archived event pages
exist, but a search index is neither a complete first-party feed nor a stable
pagination interface. It therefore cannot support a universal crawler or a
defensible assessment of first-party filters.

## Approaches attempted

- Loaded the canonical URL and `www` variant in Playwright and inspected the
  network log. Only Cloudflare challenge requests were exposed; no application
  API or calendar requests were reached.
- Waited for the browser challenge to complete, but it remained on the HTTP 403
  “Just a moment...” page.
- Tested the likely The Events Calendar REST route with explicit pagination and
  date parameters:
  `/wp-json/tribe/events/v1/events?per_page=50&page=2&start_date=2025-01-01`.
- Tested WordPress discovery and alternate REST forms: `/wp-json/`,
  `/wp-json/wp/v2/types`, `/wp-json/wp/v2/tribe_events`, and
  `?rest_route=/tribe/events/v1/events`.
- Tested HTML/calendar and archive candidates including `/events/`,
  `/pasakumi/`, `/events/?ical=1`, `/event/?ical=1`, and the events RSS feed.
- Tested Yoast and WordPress sitemap candidates including `/sitemap_index.xml`,
  `/wp-sitemap.xml`, and `/event-sitemap.xml`. The public `robots.txt` identifies
  the Yoast sitemap, but the sitemap itself is challenged.
- Inspected representative indexed event details and adjacent content. This
  established mixed-source contamination but exposed no reliable first-party
  genre/category identifiers or pagination behavior. No applicable stable
  filters could be tested because all first-party feeds were blocked.

## What would unblock implementation

Any of the following would allow a crawler to be implemented and validated:

- allowlisted non-interactive access to the site for the production crawler;
- a stable first-party event API, iCalendar feed, or sitemap exempt from the
  Cloudflare challenge;
- documented Cloudflare service credentials intended for automated access; or
- an export supplied by the venue that includes current and archived event
  detail URLs, dates, times, halls, and descriptions.

Because the accessible evidence shows a mixed calendar and no verified,
comprehensive category filter, an eventual crawler should default to
`upload_target="potential"` unless stable first-party filters are later shown to
cover the full project scope without contamination.
