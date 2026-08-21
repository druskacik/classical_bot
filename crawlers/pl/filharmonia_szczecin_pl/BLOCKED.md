<!-- crawler-factory-metadata
{"url":"https://filharmonia.szczecin.pl/en","geographic_scope":"country","country_code":"PL","reason_code":"access_blocked","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# Access blocked

## Original URL

https://filharmonia.szczecin.pl/en

## Why a crawler cannot currently be implemented

The domain resolves to `89.161.255.178`, but the origin accepts neither HTTP nor HTTPS connections from the crawler-factory environment. Requests time out without returning response headers or a response body. The same problem affects the first-party ticket host at `bilety.filharmonia.szczecin.pl`.

Search-engine results crawled on the attempt date show that the site still publishes current concert occurrences at `/repertuar` and event detail pages under `/wydarzenia/`, so this is not an empty calendar. Without direct access, however, a production parser cannot be implemented and validated against live first-party responses.

## Approaches attempted

- Playwright navigation to the supplied English URL timed out after 60 seconds before `DOMContentLoaded`; the subsequent network-request inspection also timed out, so no API request or stable filter identifier could be recovered.
- Direct HTTPS requests to the supplied URL, `/repertuar`, the `www` and wildcard-host variants, and the ticket calendar timed out without headers.
- Plain HTTP access to `/repertuar` also timed out without headers.
- Indexed first-party pages were reviewed only to confirm that concerts remain published and that detail pages expose dates, times, categories, venues, addresses, artists, works, and descriptions. The indexed repertoire UI shows the first-party category labels `KONCERTY`, `EDU`, `WYSTAWY`, and `INNE`, but cached search output cannot reveal or validate their underlying request values, pagination behavior, archive coverage, or contamination.
- The ticket calendar was considered as an alternate first-party HTML source, but it was equally unreachable and indexed excerpts do not provide reliable event-detail URLs or the full programme descriptions needed by this project.

## What would unblock implementation

Restore network access from the crawler-factory environment to `filharmonia.szczecin.pl` (and, if required, `bilety.filharmonia.szczecin.pl`), or provide a documented first-party API endpoint and any required non-secret request parameters. A retry should then inspect the repertoire category requests in Playwright, verify stable identifiers across years/months and archive pagination, inspect adjacent `EDU` and `INNE` events for eligible performance content, and validate representative home and touring event detail pages before selecting the feed and upload target.
