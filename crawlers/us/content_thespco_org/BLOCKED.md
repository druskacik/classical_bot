<!-- crawler-factory-metadata
{"url":"https://content.thespco.org/music/concert-library/","geographic_scope":"country","country_code":"US","reason_code":"no_parseable_source","attempted_at":"2026-08-16","retry_after":"2026-09-15"}
-->

# SPCO Concert Library crawler blocked

## Original URL

https://content.thespco.org/music/concert-library/

## Why a crawler cannot currently be implemented

The source publishes a classical-only archive of Saint Paul Chamber Orchestra concert programs, but it does not expose enough occurrence-level information to create valid concert records.

Each program has a `start_date_time`, an `end_date_time`, and a human-readable date range, but these describe the span of a multi-performance program rather than listing its individual performance occurrences. The source also supplies no venue or city for a program. SPCO performances take place at multiple venues in the Twin Cities, so inferring one home venue or expanding every calendar day in a program range would create unsupported records. Venue and city are required fields, and invalid events must be skipped.

## Approaches attempted

- Inspected the page with Playwright and reviewed its network requests before considering HTML parsing.
- Found and tested the first-party `GET https://content.thespco.org/api/2/combined/?format=json` endpoint. It returns 111 archived programs along with compositions and people, but programs contain only program-level start/end ranges and no venue or city.
- Opened a representative program and tested the first-party detail endpoint `GET https://content.thespco.org/api/2/programs/bachs-brandenburg-concertos-with-richard-egarr-2425/?format=json`. It adds repertoire, artists, recordings, and related media, but still has no occurrence list, venue, or city.
- Inspected the rendered concerts listing and representative detail page. The HTML is driven by the same API and displays the same date range, description, and repertoire without occurrence-level venue or city data.
- Checked the application JavaScript for another location- or venue-bearing API path; none was exposed by the concert listing or detail workflow.

The archive exposes no genre/category filter applicable to inclusion. Its concert feed is inherently classical and contains concrete SPCO concert programs, but that does not resolve the missing required occurrence fields.

## What would unblock implementation

Implementation would become possible if the source exposed an API or page containing each individual performance date/time together with its venue and city, or if SPCO provided a stable first-party mapping from every archived program to those occurrence details.
