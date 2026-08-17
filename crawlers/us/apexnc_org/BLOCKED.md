<!-- crawler-factory-metadata
{"url":"https://www.apexnc.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Crawler blocked: Apex, NC

## Original URL

https://www.apexnc.org/

## Why a crawler cannot currently be implemented

The Town of Apex site is a mixed municipal CivicEngage calendar that does publish
scrapeable event records, including classical concerts at the Halle Cultural Arts
Center. During this attempt, however, the origin server at `208.90.190.142` did
not complete a connection from the crawler environment. Both browser navigation
and direct HTTP requests timed out without returning response headers or HTML.

Without a live response it is not possible to inspect the site's actual network
requests, determine whether CivicEngage exposes a structured calendar API, verify
the stable identifier for the relevant first-party category, test past-event and
month pagination parameters, inspect representative detail-page markup, or run
the required targeted `scrape()` validation. Shipping a parser inferred only
from search-engine snippets would be an untested implementation.

## Approaches attempted

- Opened `https://www.apexnc.org/` with the Playwright MCP. Navigation timed out
  after 60 seconds before `DOMContentLoaded`; subsequent network-request and
  browser-close operations also timed out, so no API response could be examined.
- Requested the home page and calendar directly over HTTPS with browser-like
  headers, including forced IPv4. Requests to the resolved origin
  `208.90.190.142` timed out without receiving any bytes.
- Tried the canonical non-`www` host and the CivicEngage calendar route with
  first-party category and past-event query parameters. These also timed out.
- Inspected search-indexed HTML representations of the calendar and event pages.
  They confirm a mixed calendar, a first-party category named `Halle Cultural
  Arts Center Upcoming Events`, month/list query parameters, past-event controls,
  concrete event-detail URLs using `Calendar.aspx?EID=...`, and eligible events
  such as `Classical Concert Series - Duo Romantico`. The same category also
  contains movies, popular concerts, and children's programming, so it is not a
  clean classical-only filter and would require `upload_target="potential"`
  unless a more precise stable first-party filter is found on live inspection.

## What would unblock implementation

Restore HTTP access to `https://www.apexnc.org/` from the crawler environment (or
provide a reachable official mirror/API). A retry can then inspect Playwright
network traffic first, verify the exact CivicEngage category IDs across months
and past-event pagination, parse representative listing and detail pages, and
validate records without uploading or writing CSV output.
