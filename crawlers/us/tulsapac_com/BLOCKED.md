<!-- crawler-factory-metadata
{"url":"https://tulsapac.com/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# Tulsa Performing Arts Center crawler blocked

## Original URL

https://tulsapac.com/

The source is the Tulsa Performing Arts Center, a mixed performing-arts venue in
Tulsa, Oklahoma, United States. Its authoritative events calendar is linked from
the first-party site at `https://am.ticketmaster.com/tulsapac/buy`.

## Why a crawler cannot currently be implemented

Ticketmaster Account Manager returns HTTP 903 with the JSON body
`{"response":"block"}` for both the calendar and account root from this runtime.
The block occurs before the calendar application loads, so no complete event
catalogue, pagination, filters, event details, or underlying event API requests
can be inspected or fetched.

The accessible Tulsa PAC Squarespace site is not an alternative catalogue. Its
`/events` page contains general venue information and a link to Account Manager,
but no event records. The home page contains only a small promotional carousel
of featured upcoming events, which is not a comprehensive feed. The Squarespace
sitemap exposes institutional and programme pages but no individual event
detail collection or event archive. Building from the carousel would therefore
systematically omit concerts and would not satisfy the crawler's coverage
requirements.

## Approaches attempted

- Used Playwright to inspect `https://tulsapac.com/`, `/events`, the linked
  Ticketmaster Account Manager calendar, and the public Ticketmaster venue page.
- Inspected browser network traffic before attempting HTML parsing. The
  accessible Tulsa PAC pages made only Squarespace census, analytics, and popup
  requests; they exposed no event API. Account Manager returned HTTP 903 before
  it made catalogue API requests. The public Ticketmaster venue page returned
  HTTP 403.
- Requested Account Manager with a normal desktop browser user agent outside
  Playwright; it returned the same HTTP 903 JSON block.
- Inspected the accessible Squarespace HTML/JSON representations for `/` and
  `/events`. The latter has no event cards, structured event data, category
  fields, or filters; the former has only the limited featured carousel.
- Inspected `sitemap.xml` for individual concert pages and archives. None are
  exposed.
- Looked for first-party genre, category, discipline, event-type, series, and
  tag controls. No applicable filter or stable filter value is exposed on the
  accessible Tulsa PAC pages. The blocked Account Manager application could not
  be loaded far enough to test filters or their persistence across pagination
  and date ranges.

## What would unblock implementation

Any of the following would permit another attempt:

- Ticketmaster lifts or permits this runtime through its HTTP 903 bot defense.
- Tulsa PAC or Ticketmaster provides a stable, unauthenticated event feed or API
  endpoint (including pagination, event dates, venue, and detail text).
- Tulsa PAC republishes its complete calendar and event details in accessible
  first-party HTML, JSON, RSS, or iCalendar form.

Because Tulsa PAC is a mixed source and no comprehensive first-party category
filter could be verified, a future crawler using an unfiltered calendar should
use `upload_target="potential"` unless stable, comprehensive in-scope filters
can first be demonstrated.
