<!-- crawler-factory-metadata
{"url":"https://prod1.agileticketing.net/","geographic_scope":"country","country_code":"US","reason_code":"wrong_source","attempted_at":"2026-08-17","retry_after":null}
-->

# Crawler blocked: tenant source is missing

## Original URL

https://prod1.agileticketing.net/

## Why a crawler cannot currently be implemented

The supplied URL is the root of Agile Ticketing's shared, multi-tenant sales
platform, not the calendar of a particular organization. It does not identify a
single source, city, venue, or catalogue. Search-indexed pages on this host belong
to many unrelated US organizations, including churches, cinemas, theatres,
orchestras, and festivals. Combining them would create an invalid multi-source
crawler and would make the required `source` and `source_url` fields misleading.

Tenant event pages require organization-specific identifiers such as `epguid`
and an organization GUID embedded in `evtinfo`. Those identifiers cannot be
derived from the bare host. Consequently there is no defensible listing feed to
paginate, no applicable first-party category or genre filters to test, and no
meaningful choice between the `classical` and `potential` upload targets.

## Investigation performed

- Opened the supplied root with Playwright and inspected its DOM and network
  requests. The root exposed no tenant navigation, event links, API calls, or
  structured event data; it returned an Imperva/Incapsula interstitial.
- Searched for indexed pages on the exact host. Results demonstrated that the
  domain serves multiple unrelated tenants rather than one concert publisher.
- Opened a representative indexed event with Playwright. It rendered only when
  supplied with tenant-specific `epguid` and `evtinfo` values. Network inspection
  showed a server-rendered WebSales page and no tenant-discovery API that could
  reconstruct a catalogue from the root URL.
- Considered HTML parsing, but the root contains no event HTML or tenant identity.
  Parsing arbitrary indexed tenant pages would not correspond to the assigned
  source and would mix unrelated organizations.

## What would unblock implementation

Provide the canonical website or Agile WebSales listing/calendar URL for the
intended organization, including its stable tenant identifier (normally the
`epguid` query parameter). With that tenant-scoped URL, its listing pagination,
archives, category filters, and detail pages can be investigated and a crawler
can be implemented in this directory.
