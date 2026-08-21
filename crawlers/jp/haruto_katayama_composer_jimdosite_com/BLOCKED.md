<!-- crawler-factory-metadata
{"url":"https://haruto-katayama-composer.jimdosite.com/","geographic_scope":"country","country_code":"JP","reason_code":"no_current_events","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# No scrapeable concert listings

The supplied URL is the official Jimdo site for Japan-based composer Haruto
Katayama (片山 温仁):
https://haruto-katayama-composer.jimdosite.com/

A crawler cannot currently be implemented because the source exposes no
scrapeable concrete concert occurrences, including in its discoverable archive.
The indexed first-party `discography/` page lists recorded releases and release
dates, not public performances with event dates, venues, and cities. Those
release entries are outside the project's event scope and cannot produce the
required concert fields.

Investigation attempted on 2026-08-21:

- Loaded the canonical home page with Playwright and inspected its network
  requests. Cloudflare returned HTTP 403 for the document, and the trace exposed
  no XHR, fetch, JSON, GraphQL, or other event API request to reconstruct.
- Requested the canonical home page, `/robots.txt`, and `/sitemap.xml` directly
  over HTTPS (and tested the HTTP entry point). Each resolved to the same
  Cloudflare HTTP 403 response, so no sitemap or HTML event index was available.
- Searched the indexed first-party domain generally and specifically for
  concert, concerts, live, performance, schedule, news, and Japanese concert
  terminology. The only discoverable first-party content was the discography;
  no current or past concert calendar/detail pages were found.
- Checked the discoverable discography content for event-like records. Its dates
  are explicitly labelled release dates and its formats are digital/streaming,
  so they are recordings rather than concrete live performances.

No first-party genre, category, discipline, event-type, series, or tag filters
applicable to concert events were exposed. Consequently there was no feed,
pagination, or date-range filter whose identifiers could be tested, and no
upload target can responsibly be selected.

Implementation would be unblocked if the source publishes a current or archived
concert calendar (or a stable first-party API/feed) containing concrete event
dates plus defensible venues and cities, and makes that content accessible to
the crawler. A sitemap or list of currently unindexed event-detail URLs would
also allow another investigation.
