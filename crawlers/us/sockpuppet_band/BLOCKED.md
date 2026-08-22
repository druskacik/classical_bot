<!-- crawler-factory-metadata
{"url":"https://sockpuppet.band/","geographic_scope":"country","country_code":"US","reason_code":"no_current_events","attempted_at":"2026-08-22","retry_after":"2026-09-21"}
-->

# No in-scope classical concerts

## Original URL

https://sockpuppet.band/

## Why a crawler cannot currently be implemented

Sockpuppet is a solo musical project based outside Seattle, Washington. Its
first-party Live Shows archive exposes 62 performances dated from August 2019
through August 2026, but the published events are the artist's indie,
songwriter, electronic, and related live sets. Most are streamed or staged in
VRChat; the small number of physical shows are likewise nonclassical. No
concrete performance in the current calendar or retained archive meets the
project's classical-event inclusion guidance.

Many virtual entries also do not publish a defensible physical city and venue,
so they could not satisfy the required record fields even if their musical
content were in scope.

## Investigation performed

- Inspected the homepage and `/live/` with Playwright, including current and
  past performance entries and representative detail content.
- Inspected browser network traffic before considering HTML parsing. The live
  listing is server-rendered HTML; no event JSON or other structured API was
  requested. The only dynamic request was first-party analytics.
- Tested the first-party iCalendar endpoint at `/live/calendar.ics`. It is a
  structured calendar download, but it does not solve the absence of in-scope
  events and is oriented to the current calendar rather than the complete
  archive.
- Followed the archive's stable `id` pagination through the oldest retained
  entry. The archive reports 62 shows spanning 2019/08–2026/08.
- Inspected first-party tags such as `concert`, `concerts`, `full-set`,
  `short-set`, `in-person`, `VRChat`, and host/community tags. These describe
  presentation format or community rather than classical genre and include
  plainly nonclassical performances.
- Used the Live Shows first-party search with the exact queries `classical`,
  `orchestra`, and `symphonic`; all returned no results.
- Inspected the About page to resolve the source geography as the United
  States (Seattle area).

No applicable classical genre, category, discipline, event-type, series, or
tag filter is exposed. The `concert`/`concerts` tags are not classical filters.

## What would unblock implementation

A future update that publishes concrete classical, orchestral, chamber,
operatic, classical-crossover, or another qualifying performance with a real
date and defensible city and venue would permit a crawler to be implemented.
The live archive and iCalendar endpoint should then be re-evaluated for
coverage; because this is a mixed/nonclassical artist source without a
classical filter, any resulting candidate feed would normally require the
`potential` upload target unless a stable comprehensive first-party filter is
added.
