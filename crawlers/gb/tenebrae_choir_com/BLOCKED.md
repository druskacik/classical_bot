<!-- crawler-factory-metadata
{"url":"https://www.tenebrae-choir.com/","geographic_scope":"country","country_code":"GB","reason_code":"access_blocked","attempted_at":"2026-08-12","retry_after":"2026-09-11"}
-->

# Tenebrae Choir crawler blocked

## Original URL

https://www.tenebrae-choir.com/

## Why a crawler cannot currently be implemented

The site publishes a populated concert calendar at `/events` and a paginated
archive at `/events/archive`, but all first-party requests from the crawler
environment receive an HTTP 403 Cloudflare challenge page. The challenge also
blocks the site's structured and syndication endpoints, so there is currently
no stable source that a production crawler can fetch and test.

Tenebrae is a British classical choir rather than a multi-country event
publisher. Its international engagements are tours by the same organization,
so the resolved crawler geography remains GB even though individual event
records would need their actual ISO country codes.

## Approaches attempted

- Opened the home page and WordPress REST route with Playwright. Both returned
  HTTP 403 `Just a moment...` pages. Network inspection exposed only Cloudflare
  challenge traffic and no event API response.
- Tested the likely The Events Calendar REST endpoint
  `/wp-json/tribe/events/v1/events?per_page=5`; it returned the same HTTP 403
  challenge.
- Tested the calendar's iCal and RSS-style routes (`/events/?ical=1` and
  `/events/feed/`); both returned HTTP 403 challenge HTML.
- Tested `/wp-sitemap.xml`, the apex-domain redirect variant, the upcoming
  calendar, event detail pages, and archive pagination. First-party fetching
  remained blocked.
- Search-index evidence confirmed that `/events` contains concrete upcoming
  performances and `/events/archive` contains past performances across 29
  pages. Representative indexed detail pages include full programme text,
  date, start/end time, venue, locality, country, and canonical event URL, but
  search-engine copies are not a stable or acceptable production data source.

## Scope and filter investigation

The source is classical-only: it is the official performance calendar of a
classical choral ensemble, and representative upcoming and archived detail
pages are concrete choral concerts with classical repertoire. The unfiltered
upcoming feed plus the unfiltered archive would therefore be the selected feed,
with `upload_target="classical"`.

The site exposes event categories/series, including the exact category slug
`usa-tour-2026` at `/events/categories/usa-tour-2026`, but this is a tour/series
subset rather than a genre filter and would omit eligible concerts. No
applicable first-party genre, discipline, or event-type filter was exposed.
Because first-party requests were blocked, category persistence across live
pagination and REST API pagination could not be verified. Indexed archive URLs
show the stable pagination form `/events/archive/page/2`, but that route cannot
currently be fetched by the crawler environment.

## What would unblock implementation

Any of the following would allow a crawler to be implemented and validated:

- permit non-browser access to the public event calendar, archive, detail
  pages, and/or The Events Calendar REST API;
- provide a stable first-party JSON, RSS, or iCal endpoint that is exempt from
  the Cloudflare challenge; or
- allowlist the production crawler's egress address at Cloudflare.

