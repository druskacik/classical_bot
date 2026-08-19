<!-- crawler-factory-metadata
{"url":"https://www.nashvillesymphony.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-19","retry_after":"2026-09-18"}
-->

# Nashville Symphony crawler blocked

## Original URL

https://www.nashvillesymphony.org/

The organization and its performances are based in Nashville, Tennessee, so the resolved country code is `US`.

## Why a crawler cannot currently be implemented

The public main site is accessible and publishes parseable concert detail pages, but it does not expose an exhaustive concert listing. Its homepage currently links only three featured September 2026 concerts, so crawling those links would systematically omit other in-scope performances. The site's canonical calendar redirects to `https://tickets.nashvillesymphony.org/events`, where automated access receives an Imperva "Additional security check is required" page with an hCaptcha challenge.

No stable, exhaustive first-party feed could therefore be reached. The accessible season-package page confirms that the source covers classical, pops, family, movie-with-live-orchestra, and special concerts, all potentially in scope under the project guidance. A featured-only or narrowly classical-only implementation would not provide the requested coverage.

## Approaches attempted

### API and network investigation

- Inspected homepage and ticket-calendar network traffic with Playwright.
- The main site calls `https://tickets.nashvillesymphony.org/api/session/sessionkey`, which returns only an encrypted session key and no event data.
- Navigating to the ticket calendar produced the Imperva/hCaptcha security page before the application or its event-data requests could load, so no event API request could be reconstructed.
- Tested the first-party package links and exact values exposed for the 2026/27 season: `k=FullClassical&seasonid=388`, `k=HalfClassical&seasonid=388`, `k=Pops`, `k=Movie`, and fixed package IDs `1121` (Thursday Classical), `1120` (Matinee), and `1111` (Family). These identifiers are present as stable links on the first-party season page, but the destinations are protected by the same ticket-domain challenge. Pagination and date-range persistence could not be tested because listing content never loaded.
- The site also embeds Google Custom Search (`cx=014690481841676536283:wyphxstayxw`). This is an external search index, not a comprehensive first-party event feed; its results and pagination cannot establish complete coverage or stable category filtering.

### HTML investigation

- Inspected the homepage, season-ticket page, series-package page, and representative concert detail pages.
- Representative detail pages contain concrete occurrence dates, times, venue, performers, descriptive prose, and full programme text and would be parseable once an exhaustive URL source is available.
- The homepage contained only three featured concert-detail links, not the full calendar.
- Parent paths such as `/tickets/concert/2026-2027-season/`, `/sitemap.xml`, and `/robots.txt` redirect to the homepage rather than providing a listing or sitemap.
- Direct non-browser requests to the ticket calendar return the same Imperva block page.

## Filters and feed decision

The first-party season page exposes package filters for Classical, Pops, Family, Movie, Matinee, and Specials-related programming. The Classical package alone is too narrow for project scope because eligible family concerts, live-to-picture movie concerts, crossover/pops performances featuring the orchestra, and specials may be omitted. Since the filtered destinations could not be loaded, their contamination, completeness, pagination, and date-range behavior could not be verified. No feed or upload target was selected because no sufficiently complete scrapeable listing is currently available.

## What would unblock implementation

Any of the following would allow a crawler to be built:

- removal or allow-listing of automated read access to the ticket calendar;
- a documented or observable event-search API that returns the calendar without an interactive challenge;
- a first-party sitemap or exhaustive server-rendered archive of concert detail URLs; or
- an accessible combined set of stable first-party series/category feeds covering Classical, Pops/crossover, Family, Movie/live-score, and Specials across pagination and date ranges.
