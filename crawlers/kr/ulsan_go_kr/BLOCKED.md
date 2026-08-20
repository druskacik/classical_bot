<!-- crawler-factory-metadata
{"url":"https://www.ulsan.go.kr/u/rep/main.ulsan","geographic_scope":"country","country_code":"KR","reason_code":"no_current_events","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# Crawler blocked: no scrapeable concerts

## Original URL

https://www.ulsan.go.kr/u/rep/main.ulsan

The municipal home page links to the first-party monthly cultural-events page at
`https://www.ulsan.go.kr/u/culture/transfer/MonthEvntCulture/list.ulsan?mId=001003001001000000`.
That calendar currently exposes no concert records, and it provides no date or
archive controls with which to retrieve past events.

## Investigation

- Playwright network inspection found no event JSON or other structured API.
  The calendar is rendered in the initial HTML response; the only dynamic
  requests observed were analytics requests.
- The first-party event-type controls were tested. Their exact values are
  `강좌` (class/course), `공연` (performance), `기타` (other), `전시`
  (exhibition), `교육` (education), `체험` (experience), and `영화` (film).
  Selecting `공연` submits the stable POST field `searchSj2=공연`, but the
  filtered response contains zero items and no pagination links.
- The unfiltered HTML currently contains only an evergreen library book-pack
  distribution item dated from 2024 until supplies run out and a template/example
  row. Neither is a concert or a concrete eligible performance.
- The HTML exposes no detail-page links, event identifiers, genre/discipline
  filters below the broad performance type, calendar month/year parameter, or
  accessible archive. Consequently there are no representative concert detail
  pages or historical occurrences to parse and validate.

## What would unblock implementation

A populated `공연` feed (with concrete dates, venues, and cities), a documented
or discoverable archive/date parameter, or a first-party event API containing
current or past performance records would make it possible to implement and
validate a crawler. Because `공연` is a mixed performance category rather than a
classical-only classification, any eventual crawler should use the `potential`
upload target unless the source later exposes sufficiently comprehensive and
reliable classical-scope filters.
