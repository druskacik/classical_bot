<!-- crawler-factory-metadata
{"url":"https://kyso.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-16","retry_after":"2026-09-15"}
-->

# Crawler blocked by Cloudflare

The original source is https://kyso.org/, the official website of the Kentucky
Symphony Orchestra in the United States.

A crawler cannot currently be implemented because Cloudflare returns HTTP 403
with a `Just a moment...` challenge page to automated browser and HTTP clients.
The challenge does not resolve in Playwright, and the same protection covers
the calendar HTML and structured WordPress endpoints. A production crawler
would therefore be unable to retrieve authoritative event data.

## Approaches attempted

- Loaded the homepage in Playwright and waited for the Cloudflare challenge.
  The network log contained only the blocked document and Cloudflare challenge
  requests; no application event API was exposed.
- Tested the `www` hostname and the HTTP-to-HTTPS route in Playwright. Both
  resolved to the same challenge.
- Tested calendar HTML routes `/events/` and `/concerts/` in Playwright. Both
  returned the Cloudflare challenge rather than event markup.
- Tested the WordPress REST discovery endpoints `/wp-json/` and
  `/wp-json/wp/v2/types`, plus `/robots.txt` and `/sitemap_index.xml`.
  No usable discovery document or sitemap was available.
- Reconstructed the likely The Events Calendar API from indexed first-party
  pages and tested `/wp-json/tribe/events/v1/events`, including a bounded
  `start_date`/`end_date` query. Direct HTTP requests also returned HTTP 403.
- Tested the calendar iCalendar-style routes `/events/?ical=1` and
  `/?post_type=tribe_events&ical=1&eventDisplay=list`; they did not provide a
  reliably accessible event feed.

Search-indexed copies show that the source publishes concrete orchestral
performances and retains past events. They also show that the unfiltered
calendar can contain non-performance records such as string auditions and a
recorded virtual pass. First-party taxonomy routes observed in indexed pages
include the `concerts` tag, `datenight` tag, and `summer-series` category, but
they could not be fetched live to verify stable identifiers, pagination,
date-range behavior, coverage, or contamination. Consequently no filtered feed
can be safely selected at this time.

## What would unblock implementation

Any stable first-party route that is accessible to the production runtime would
unblock the crawler: allowlisting the crawler, relaxing the Cloudflare challenge
for calendar/REST endpoints, or publishing an accessible JSON, iCalendar, RSS,
or HTML event feed. Once access is restored, the Tribe Events REST API should be
tested first across past and future date ranges and all pages. If the available
taxonomy filters cannot comprehensively isolate in-scope performances, the
calendar should be scraped to the `potential` upload target so auditions,
recorded-only products, and other ambiguous entries receive classification.
