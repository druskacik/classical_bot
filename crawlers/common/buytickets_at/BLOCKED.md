<!-- crawler-factory-metadata
{"url":"https://buytickets.at/","geographic_scope":"multi_country","country_code":null,"reason_code":"access_blocked","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# Crawler blocked

## Original URL

https://buytickets.at/

The URL redirects to `https://www.tickettailor.com/events/`, Ticket Tailor's
global, mixed-event discovery site. It is not tied to one organizer or country,
so the resolved geographic scope is multi-country and the country code is null.

## Why implementation is currently blocked

Ticket Tailor returns an HTTP 403 Cloudflare challenge to the browser for the
global event catalogue. The same challenge blocks organizer box-office pages,
individual event pages, the XML sitemap, and category discovery pages. The
challenge completes a Cloudflare request but does not grant access to the event
HTML. No catalogue or event-data API request is made before the block, leaving
no structured response that a production crawler can reproduce.

The source is also mixed. The only applicable first-party discovery filter that
could be identified was the `live-music` category at
`/discover/categories/live-music`. Its own description covers a broad mixture
including jazz and popular live music, and it would not comprehensively cover
eligible opera, ballet/dance, or other classical performing arts. Filter
pagination and date-range persistence could not be tested because the category
page is blocked at its initial request. An eventual crawler should therefore use
the potential-event upload path unless stable, comprehensive first-party filters
can be verified after access is restored.

## Approaches attempted

- Opened the supplied domain in Playwright and followed its redirect to the
  Ticket Tailor global `/events/` catalogue; the final response was HTTP 403.
- Inspected Playwright network traffic. It contained the blocked document and
  Cloudflare challenge requests, but no event catalogue/API response.
- Opened a representative organizer box office and the first-party
  `/discover/categories/live-music` page; both were blocked with HTTP 403 before
  event data loaded.
- Opened a representative event-detail route; it was blocked in the same way.
- Retrieved `robots.txt`, which is accessible and advertises
  `https://www.tickettailor.com/sitemap.xml`, then attempted that sitemap; the
  sitemap was also blocked with HTTP 403.
- Checked indexed public results to confirm that Ticket Tailor hosts concrete
  events across many organizers and countries. Search-index excerpts are not a
  stable or complete source and cannot support a universal crawler.

## What would unblock implementation

Reliable non-interactive access to the Ticket Tailor catalogue, sitemap, and
detail pages, or a documented/public catalogue API that enumerates all events
without organizer credentials. Once available, investigation must verify stable
pagination and date ranges, discover and combine all relevant first-party
categories, inspect adjacent categories and representative detail pages, and
emit an ISO country code for every event.
