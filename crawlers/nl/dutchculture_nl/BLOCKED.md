<!-- crawler-factory-metadata
{"url":"https://dutchculture.nl/en/organisation/yulianna-beziazychna","geographic_scope":"country","country_code":"NL","reason_code":"no_parseable_source","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# No parseable concert source

The supplied URL is the DutchCulture Cultural Database profile for the
Netherlands-based classical pianist Yulianna Beziazychna:

https://dutchculture.nl/en/organisation/yulianna-beziazychna

The profile has an archive, but it does not expose enough information to create
valid concert records. Its four activities consist of two awards and two rows
labelled `Concert`. The concert entities are placeholder records titled `x` and
cover whole competition ranges (27 January–4 February 2024 and 14–22 October
2024), rather than identifying a concrete performance date. They provide no
performance time, programme, description, or more precise occurrence date.
Choosing the first or last day of either range would invent a concert date, and
using `x` as a public concert title would preserve a database placeholder rather
than a meaningful event title.

## Investigation performed

- Inspected the rendered organization page and its complete activities table.
- Inspected the Drupal settings and network-facing configuration. The page uses
  the first-party Drupal Views AJAX endpoint `/en/views/ajax`; its view is
  `activities`, display `activities_by_artist`, with organization argument
  `1693129`.
- Tested the first-party country filter values `AT`, `CH`, and `All`, and the
  exposed date-range parameters `field_date_range_end_value` and
  `field_date_range_value`. These are stable query parameters, but filtering
  cannot add missing occurrence data. The archive has only four rows, so it has
  no pagination to validate beyond the initial page; a synthetic `page=1`
  request yielded no additional usable catalogue and was challenged by
  Cloudflare.
- Probed Drupal JSON endpoints (`/jsonapi` and
  `/jsonapi/node/activity/<id>`); JSON:API is not exposed (404).
- Opened the underlying first-party Drupal entity IDs. The concert entities
  resolve to `/en/activity/x-23326` and `/en/activity/x-23327` and confirm the
  placeholder title, `Classical Music` discipline, `Concert` event type,
  competition venue, city/country, and only the broad competition date range.
  The adjacent entities resolve to `First Prize` award pages and are not
  concerts.
- Inspected the only relevant first-party taxonomy shown on the profile:
  discipline `Classical Music` and event types `Concert` and `Award`. There is no
  broader eligible event feed on this artist-specific page. The concert filter
  is uncontaminated by the award rows when event type is checked, but its two
  results remain non-concrete placeholders.
- Inspected HTML as a fallback. It contains the same incomplete records and no
  hidden concert title or exact performance date.

Playwright MCP was not available in the execution environment, so browser
network reconstruction could not be performed through that requested tool.
Equivalent read-only inspection of the delivered HTML, Drupal runtime settings,
first-party endpoints, query parameters, and underlying entity pages was used.

## What would unblock implementation

A first-party update supplying a concrete performance date and meaningful title
for each concert would make the archive scrapeable. A stable API or linked
programme page containing those fields would also unblock implementation. If
future activities on this profile contain concrete concert occurrences, the
source should be retried. Because this is a Netherlands-based artist profile,
its source geography is `NL`; foreign activity locations are tours and do not
make the organization itself a multi-country source.
