<!-- crawler-factory-metadata
{"url":"https://www.teatrocomunaleferrara.it/","geographic_scope":"country","country_code":"IT","reason_code":"access_blocked","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# Access blocked

## Original URL

https://www.teatrocomunaleferrara.it/

The source is the mixed-programme Teatro Comunale di Ferrara in Ferrara, Italy.
It publishes opera, dance, theatre, children's events, meetings, and other
performances, while its concert navigation links to the separate Ferrara Musica
website.

## Why a crawler cannot currently be implemented

The canonical host resolves to `136.243.72.62`, but it does not complete an HTTP
or HTTPS connection from the crawler-factory runtime. Browser navigation and
direct requests time out without response headers or a response body. The same
failure affects the public WordPress REST endpoints, so a production crawler
deployed from this environment would be unable to obtain either listings or
event details. A third-party rendering proxy can see the site, but depending on
that proxy would not be a first-party, production-safe scraping implementation.

## Approaches attempted

- Playwright navigation to the canonical HTTPS homepage timed out while waiting
  for `domcontentloaded`; because the navigation never completed, no page
  network-request list could be captured.
- Direct IPv4 requests were attempted against the canonical host over both HTTP
  and HTTPS with browser-style headers. They timed out before receiving headers.
- The homepage and the common WordPress REST routes `/wp-json/`,
  `/wp-json/wp/v2/types`, and `/wp-json/wp/v2/spettacolo` were investigated.
  The canonical routes are inaccessible directly from this runtime.
- An external read-only renderer confirmed that the site currently contains
  concrete performances and exposes a WordPress custom post type named
  `spettacolo`. Its first-party taxonomies are `tipo`, `stagione`, and
  `location`.
- Exact `tipo` values inspected were `Prosa` (17), `Extra` (18),
  `Opera&Danza` (19), `Festival della Poesia` (20), `Teatro Ragazzi` (21),
  `CittàTeatro` (22), `Festival di Danza Contemporanea` (23), `Incontri`
  (36), `Incontri con le compagnie` (37), `Prima della prima` (38),
  `Scuola all’opera` (39), `Storie d'Opera` (40),
  `Dietro le quinte dell’opera` (41), `Musica e Scuola` (72),
  `Interno Verde Danza` (87), and `Site Specific` (94).
- Exact season values inspected were `Stagione 2024-2025` (25),
  `Stagione 2025-2026` (73), and `Stagione 2026-2027` (92). The API advertises
  stable taxonomy queries such as `/wp-json/wp/v2/spettacolo?tipo=19`, but
  persistence through live pagination and date coverage could not be verified
  because direct requests never return.
- Representative externally rendered detail pages included `L'ANNO CHE VERRÀ`
  (`Opera&Danza`), which is an eligible orchestra-and-chorus opera/crossover
  performance, and `HER` plus `LA CITTÀ CHE DANZA` (`Festival di Danza
  Contemporanea` / `Site Specific`), showing that adjacent dance categories are
  not uniformly in scope. Therefore `Opera&Danza` alone would be too narrow,
  while an unfiltered or broadly combined candidate feed would require
  `upload_target="potential"`.
- HTML parsing was considered from the rendered event pages: dates, ticket
  times, venue labels, and long programme/body text are visibly present. Raw
  first-party HTML could not be downloaded from the canonical host for selector
  validation or parser testing.

## What would unblock implementation

Restore network access from the crawler-factory/production egress addresses (or
remove the source-side firewall/drop rule) so the canonical homepage, event
detail pages, and WordPress REST API return normally. Once reachable, the REST
taxonomy filters must be tested across all pages and seasons, representative
adjacent categories must be checked, and the HTML detail parser must be
validated against live multi-date performances. Given the mixed and ambiguous
coverage observed, the safe initial feed should be uploaded as `potential`
unless comprehensive first-party category filtering can be proven.
