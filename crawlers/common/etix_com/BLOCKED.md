<!-- crawler-factory-metadata
{"url":"https://www.etix.com/ticket/","geographic_scope":"multi_country","country_code":null,"reason_code":"access_blocked","attempted_at":"2026-08-14","retry_after":"2026-09-13"}
-->

# Etix crawler blocked

## Original URL

https://www.etix.com/ticket/

## Why a crawler cannot currently be implemented

Etix is a mixed-event, multi-country ticket marketplace. The canonical event site returns HTTP 403 from an AWS WAF challenge in the available browser environment. The response contains only “Please enable JS and disable any ad blocker,” so it provides neither listings nor usable pagination, categories, locations, or event detail data.

Search-engine indexing confirms that Etix publishes concrete classical concerts as well as many unrelated event types, but indexed results are not a complete or stable first-party feed. Using them would systematically miss events and would not provide a verifiable, comprehensive set of project-scope filters. An unfiltered or keyword-selected subset therefore cannot safely be uploaded as either a complete classical feed or a defensible potential-event candidate feed.

## Approaches attempted

- Opened the canonical `https://www.etix.com/ticket/` page with Playwright and inspected its network requests. The only dynamic requests were AWS WAF token/challenge requests; the document request remained HTTP 403 and no event API request was made.
- Opened a representative, currently indexed classical event-group URL (`/ticket/o/og/2111/coplandbritten-shaw-2026`) with Playwright. It returned the same HTTP 403 response, preventing inspection of its detail data or underlying requests.
- Opened the legacy first-party calendar endpoint (`/ticket/online3/calendar.jsp?venue_id=35`) with Playwright. It was also blocked with HTTP 403.
- Investigated publicly indexed first-party pages for search, calendar, event-group, and tour routes. These demonstrate venue-specific identifiers and concrete event pages, but no discoverable global API or comprehensive classical/opera/ballet/choral/crossover category feed with stable pagination and date-range parameters.
- Checked HTML parsing as a fallback. The accessible response is only the WAF error page and contains no parseable concert records. Search-index snippets are incomplete third-party snapshots and are not suitable crawler input.

No applicable first-party genre, category, discipline, event-type, series, or tag filter could be tested because the WAF prevented the application and its network data from loading. Consequently, filter persistence across pagination could not be verified.

## What would unblock implementation

One of the following is required:

- allowlisted or otherwise legitimate automated access to the Etix listing and detail pages;
- documented access to a comprehensive Etix discovery API, including category, country/location, date-range, and pagination fields; or
- a stable first-party export/feed that enumerates all candidate events and supplies event detail URLs, dates, venues, cities, and countries.

Once access is available, the source should default to `upload_target="potential"` unless comprehensive first-party scope filters can be identified and verified across pagination and representative adjacent categories.
