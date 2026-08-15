<!-- crawler-factory-metadata
{"url":"https://aicmf.org/","geographic_scope":"country","country_code":"US","reason_code":"no_parseable_source","attempted_at":"2026-08-15","retry_after":"2026-09-14"}
-->

# Amelia Island Chamber Music Festival crawler blocked

## Original URL

https://aicmf.org/

## Why a crawler cannot currently be implemented

The source is the US-based Amelia Island Chamber Music Festival. Its live event
calendar currently publishes only a July 22–August 2, 2027 fundraising river
cruise. That listing has no concert performance, venue, or city and is not a
valid concert occurrence under the project scope.

The site's 2025–2026 concerts remain available only in a linked 46-page PDF
brochure. The repository has no PDF text-extraction dependency or system PDF
utility, and the task does not permit adding dependencies. The live HTML and
public WordPress APIs no longer expose those concert occurrences as structured
records. Consequently there is no production-safe, universal source from which
the required title, date, URL, venue, city, and description fields can currently
be extracted.

## Approaches attempted

- Inspected browser network requests with Playwright. The site uses The Events
  Calendar for WordPress; no separate concert XHR or GraphQL feed was observed.
- Queried the first-party endpoint
  `/wp-json/tribe/events/v1/events?per_page=50&start_date=2000-01-01&end_date=2035-12-31`.
  It returned one item and one page: the non-concert river cruise. Its category
  and tag arrays were empty.
- Queried `/wp-json/wp/v2/tribe_events?per_page=100`. It likewise returned only
  that single item, so pagination does not expose deleted past occurrences.
- Tested the HTML past-events view at
  `/events/photo/?eventDisplay=past`; it redirects to the current events view and
  does not expose an archive.
- Inspected the current season/calendar HTML and the external Arts People
  ticketing page. Neither contains the past concert records.
- Tested WordPress search and discovered references to old Elementor season
  templates, but their REST detail endpoints return HTTP 401 and do not provide
  a stable public event feed.
- Downloaded and inspected the linked 2025–2026 PDF brochure under `/tmp`. It
  contains concrete past concerts, dates, venues, times, and descriptions, but
  those values are not present in scrapeable HTML or JSON. PDF extraction was
  possible only with a temporary investigation-only library unavailable to the
  production crawler.

## Filters and feed assessment

No applicable first-party genre, category, discipline, event-type, series, or
tag filter is exposed. The Events Calendar API returns empty `categories` and
`tags` for its sole record, so there are no filter values to test across pages
or date ranges. The organization is principally a chamber-music festival, but
its event feed can include fundraising/travel records; an unfiltered live feed
would therefore require `upload_target="potential"` if it contained candidates.
At present it contains no valid candidate with the required venue and city.

## What would unblock implementation

Implementation can proceed when the 26th-season concerts are published as
individual HTML or WordPress event records (expected around the announced
September 2026 ticket release), when the site restores a structured past-event
archive, or when an approved PDF parsing dependency becomes available in the
production environment.
