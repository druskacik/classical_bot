<!-- crawler-factory-metadata
{"url":"https://www.catholicsongbook.com/","geographic_scope":"country","country_code":"US","reason_code":"wrong_source","attempted_at":"2026-08-19","retry_after":null}
-->

# Crawler blocked: supplied domain is an unrelated, repurposed source

## Original URL

https://www.catholicsongbook.com/

## Why a crawler cannot currently be implemented

The supplied domain no longer presents a Catholic songbook, a concert presenter,
or a classical-music event calendar. It currently serves an Indonesian-language
SCBET88 gambling/affiliate page layered over marketplace navigation. There are no
concrete concert occurrences, dates, venues, cities, programmes, or archives from
which valid crawler records can be produced. This is a repurposed/unrelated domain,
so the appropriate reason is `wrong_source`, rather than an empty concert calendar.

## Investigation performed

- Opened the canonical HTTPS URL in Playwright. It loaded directly at the same URL
  with the title “SCBET88: Review Bersama Situs Scbet88 Paling Tepat Janji Cuan Hari
  Ini”; there was no transient redirect or access-interstitial.
- Inspected Playwright network traffic before considering HTML parsing. No concert
  or event API, JSON feed, WordPress REST endpoint, or calendar request was exposed.
  The relevant requests were the document, analytics/advertising traffic, a failed
  local `/track` request, and marketplace support resources.
- Inspected the rendered HTML text and links. The content advertises casino, poker,
  slot, deposits, withdrawals, login, and registration. Navigation also exposes
  Bonanza-style marketplace routes such as item search, fashion categories, account
  dashboards, and shopping/selling help—not performance listings.
- Looked for first-party genre, category, discipline, event-type, series, and tag
  filters. No event filters exist, so there are no filter values or pagination/date
  behavior to test and no representative detail pages to inspect.
- Checked for current and archived concerts in the exposed site navigation and page
  content; none are available.

## What would unblock implementation

Provide the current official URL for the intended concert-presenting organization,
or restore this domain to a first-party site/API containing concrete concert
listings with dates and locations. Once such a source exists, its API/network feed
and HTML archive can be evaluated for complete in-scope coverage and an appropriate
`classical` or `potential` upload target.
