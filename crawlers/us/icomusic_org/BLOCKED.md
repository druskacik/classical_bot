<!-- crawler-factory-metadata
{"url":"https://www.icomusic.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-18","retry_after":"2026-09-17"}
-->

# Access blocked

## Original URL

https://www.icomusic.org/

The source is the Indianapolis Chamber Orchestra, a US organization based in
Indianapolis, Indiana. Search-engine indexing confirms that the site currently
publishes concrete classical performances and archives, so this is not an empty
calendar.

## Why a crawler cannot currently be implemented

Every first-party request made from the crawler environment returns HTTP 403
with the page title `Blocked by Geographic Restriction`. The restriction applies
before the calendar HTML or API response is returned. A production crawler built
and tested from this environment would therefore have no scrapeable first-party
source and would fail on every run.

## Approaches attempted

- Loaded the canonical HTTPS URL and its HTTP/non-`www` variants with Playwright;
  all resolve to the same geographically blocked HTTPS response.
- Inspected Playwright network traffic. The origin request itself returns 403,
  and no calendar/API request is initiated.
- Requested `/robots.txt` and `/sitemap_index.xml`; both are covered by the same
  geographic restriction.
- Probed the site's WordPress REST discovery and content routes (`/wp-json/`,
  `/wp-json/wp/v2/types`, and `/wp-json/wp/v2/pages?per_page=5`); each returns
  the same 403 response.
- Investigated indexed calendar and detail results. They identify a WordPress
  Events Calendar installation with list pages under `/concerts-and-events/`
  and event details under `/concerts/<slug>/`, including dates, times, venues,
  descriptions, and repertoire. Search results are not a stable or complete
  first-party feed and cannot support a production crawler.

No first-party genre, category, discipline, event-type, series, or tag filter
could be tested directly because all relevant first-party HTML and API routes
are blocked. Indexed results show season-category URLs, but their identifiers,
pagination behavior, archive coverage, and contamination could not be verified.
The organization itself is classical-only, so an accessible complete event feed
would normally be eligible for `upload_target="classical"`.

## What would unblock implementation

Allow the crawler's deployment region/IP through the site's geographic firewall,
or provide an authorized first-party calendar/API endpoint that is reachable
from that environment. Once access is restored, the calendar's pagination and
archive date ranges, WordPress/Events Calendar API, category identifiers, and
representative event details can be verified and a crawler implemented.
