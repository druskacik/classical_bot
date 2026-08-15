<!-- crawler-factory-metadata
{"url":"https://philharmonia.lviv.ua/","geographic_scope":"country","country_code":"UA","reason_code":"access_blocked","attempted_at":"2026-08-15","retry_after":"2026-09-14"}
-->

# Crawler blocked

## Original URL

https://philharmonia.lviv.ua/

## Why implementation is currently blocked

The source is the Lviv National Philharmonic, based in Lviv, Ukraine, so the
resolved country is `UA`. Its concert catalogue is currently protected by a
Cloudflare managed challenge. Both a browser session and ordinary production-
style HTTP requests receive HTTP 403 responses for the home page, event pages,
event archive, sitemaps, WordPress REST API, and WordPress AJAX endpoint.
Consequently, there is no reliable way to discover all current and archived
event occurrences, follow pagination, or fetch the required event details.

The general WordPress RSS endpoint at `/feed/` is reachable, but it contains
only ten recent news/announcement posts. Representative entries include news
reports and multi-concert overview articles, so it is neither a complete event
feed nor safe to treat as concrete concert occurrences.

## Approaches attempted

- Loaded the canonical URL with Playwright and inspected its network requests.
  Navigation ended at a Cloudflare `Just a moment...` page with HTTP 403; the
  only subsequent requests were Cloudflare challenge and Turnstile resources,
  not an application API.
- Waited for the browser challenge to complete, but it remained blocked.
- Probed the first-party WordPress REST routes (`/wp-json/`,
  `/wp-json/wp/v2/types`, and `?rest_route=/wp/v2/types`) and
  `/wp-admin/admin-ajax.php`; all returned the Cloudflare challenge.
- Probed the advertised sitemap endpoints (`/sitemap.xml`,
  `/news-sitemap.xml`, `/sitemap.rss`, and `/wp-sitemap.xml`) and the event
  archive at `/archive-events/`; all were blocked.
- Tested likely event RSS variants (`/event/feed/`, `/events/feed/`,
  `/archive-events/feed/`, and `?post_type=event&feed=rss2`); all were blocked.
- Inspected the reachable `/feed/` RSS output. It is an incomplete mixed news
  feed rather than an event catalogue and includes non-event and overview
  content.

## Filters and source scope

No applicable, stable first-party event filter could be tested because the
event catalogue and API are inaccessible. The indexed site exposes labels such
as the `kids-events` event category, but its feed is blocked and the label alone
would omit other in-scope orchestral, chamber, choral, contemporary, crossover,
and family performances. The institution is predominantly classical but its own
description also mentions jazz, pop, exhibitions, and masterclasses; therefore
an unfiltered or announcement-derived feed could not safely upload directly as
classical. If access becomes available and no comprehensive event filters are
exposed, the appropriate target would be `potential`.

## What would unblock implementation

Any stable first-party endpoint that is accessible to unattended HTTP clients
and lists the complete event catalogue (including pagination and archives),
plus accessible event detail pages or structured detail responses. Alternatively,
the site could exempt its public event API, sitemap, or event RSS feed from the
Cloudflare challenge.
