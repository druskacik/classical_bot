<!-- crawler-factory-metadata
{"url":"https://aldboroughfestival.co.uk/","geographic_scope":"country","country_code":"GB","reason_code":"access_blocked","attempted_at":"2026-08-18","retry_after":"2026-09-17"}
-->

# Northern Aldborough Festival crawler blocked

## Original URL

https://aldboroughfestival.co.uk/

## Why a crawler cannot currently be implemented

The source is the Northern Aldborough Festival in England, so its resolved
geography is GB. The website publishes concrete current and archived events,
but all live requests from the crawler environment are redirected to a
SiteGround `/.well-known/sgcaptcha/` Robot Challenge Screen. The challenge
prevents retrieval of listing pages, event detail pages, WordPress metadata,
and API responses. A production crawler therefore cannot currently obtain or
validate the required title, date, URL, venue, city, and description fields.

The indexed event archive shows that this is a mixed source: it contains
classical concerts as well as jazz, talks, and pop/tribute events. No applicable
first-party genre, category, discipline, event-type, series, or tag filter was
discoverable. If access becomes available, the unfiltered candidate archive
must therefore use `upload_target='potential'` unless stable and comprehensive
first-party filters can then be identified and verified.

## Approaches attempted

- Loaded the home page with Playwright and inspected its network traffic. Only
  the robot-challenge document and challenge assets were returned; no event API
  request was made by the application.
- Tested the public event/archive paths `/tc-events/`, `/events/`, and
  `/programme/`. Each was redirected to the same challenge before HTML event
  content could be inspected.
- Tested WordPress discovery and API paths `/wp-json/`,
  `/wp-json/wp/v2/types`, and `/?rest_route=/wp/v2/tc_events`. Each was blocked
  by the challenge, so post types, pagination, filters, and date-range behavior
  could not be reconstructed or verified.
- Tested `robots.txt` and `sitemap.xml`, plus HTTP and `www` URL variants. These
  were also challenge-protected.
- Search-engine results confirmed concrete 2026 event detail pages and the
  paginated `/tc-events/` archive, including both classical and nonclassical
  entries, but search indexes are not a stable first-party scraping interface.

## What would unblock implementation

Allowlisting the production crawler's egress address, disabling the SiteGround
challenge for public event and WordPress API paths, or providing a stable
first-party JSON/HTML feed accessible without an interactive challenge would
allow the archive, pagination, detail fields, and any category filters to be
investigated and a crawler to be implemented and tested.
