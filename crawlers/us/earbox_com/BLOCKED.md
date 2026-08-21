<!-- crawler-factory-metadata
{"url":"https://earbox.com/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# Earbox crawler blocked

## Original URL

https://earbox.com/

The canonical source is John Adams's official website. Its relevant concert source is the classical-only **Performances** page at `https://www.earbox.com/performances/`. The site's organization and composer are based in the United States, so the resolved geography is `US`; international performances would still need their own per-record country codes if access is restored and a crawler is built.

## Why implementation is currently blocked

SiteDistrict's security layer returns HTTP 403 **Access Denied** to this execution environment. The response explicitly says the request was blocked as resembling automated traffic. It prevents access to both event HTML and WordPress API responses, so there is no parseable first-party event source from which a working crawler can be implemented or validated.

Public search indexing confirms that `/performances/` is titled **Performances - Earbox - John Adams** and contains an **Upcoming Performances** section, but the indexed representation does not expose concrete event records with the required date, venue, and city fields. Search snippets are not a stable or complete source for a production crawler.

## Approaches attempted

- Loaded `https://earbox.com/` with Playwright and received HTTP 403. Its network log contained only the denied document and the security provider's robot image; no application, XHR, fetch, GraphQL, or event API request was made.
- Loaded `https://www.earbox.com/performances/` with Playwright and received the same HTTP 403 response before any event page or client-side request could load.
- Requested the homepage and performances page using both canonical host variants and HTTP/HTTPS; all were blocked.
- Probed the standard WordPress REST API root at `https://www.earbox.com/wp-json/` and the likely page endpoint `https://www.earbox.com/wp-json/wp/v2/pages?slug=performances`; both returned the same HTTP 403 denial.
- Checked current public search indexing for Earbox performances, concerts, events, and archives. It identifies the relevant performances page and numerous work/archive pages, but provides no stable structured feed or complete scrapeable occurrences.

No first-party genre, category, discipline, event-type, series, or tag filters could be tested because access is denied before the site or API loads. Consequently, pagination and date-range persistence could not be evaluated. The intended source would be the dedicated `/performances/` feed rather than work-category archives, but its coverage and possible overview-page contamination cannot be verified while blocked.

## What would unblock implementation

Any of the following would allow a retry:

- SiteDistrict permits this crawler environment's IP/ASN or provides an allowlisted endpoint.
- The site publishes an accessible first-party calendar feed (JSON, iCalendar, RSS, or WordPress REST endpoint).
- A browser session from an unblocked network can inspect the performances page and its network requests, followed by access from the production crawler environment to the discovered stable endpoint.

After access is restored, the retry should inspect network requests first, verify all pagination and date ranges including archives, then parse representative detail pages and emit per-event geography for John Adams performances worldwide.
