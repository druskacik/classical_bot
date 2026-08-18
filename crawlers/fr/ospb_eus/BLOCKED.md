<!-- crawler-factory-metadata
{"url":"https://www.ospb.eus/","geographic_scope":"country","country_code":"FR","reason_code":"access_blocked","attempted_at":"2026-08-18","retry_after":"2026-09-17"}
-->

# Crawler blocked by the source firewall

## Original URL

https://www.ospb.eus/

The source is the Conservatoire du Pays Basque / Orchestre Symphonique du Pays
Basque, based in France. Its programme includes performances in several French
Basque Country cities and occasional touring dates, so the resolved source
geography is France rather than a multi-country source.

## Why implementation is currently blocked

Every tested first-party entry point returns HTTP 403 with a MalCare Firewall
page stating that the request was blocked for "Malicious Activities". The
response contains no event HTML or structured data. This affects browser and
ordinary HTTP clients, so there is no source endpoint that a production crawler
can currently retrieve reliably from this environment.

Search-engine indexing shows that the site still publishes concrete current and
archived concerts, but indexed snippets and cached search results are not a
stable or complete first-party feed and cannot support a universal crawler.

## Approaches attempted

- Loaded `https://www.ospb.eus/` with Playwright and inspected its network
  requests and response body. The sole document request returned HTTP 403 and a
  MalCare Firewall block page.
- Tested the non-`www` hostname. It returned the same HTTP 403 block page.
- Probed the site's WordPress REST API root and representative `wp/v2` routes,
  including post-type discovery and a likely cultural-season endpoint. All
  returned the same HTTP 403 page rather than JSON.
- Tested the first-party WordPress RSS feed at `/feed/`; it was also blocked.
- Checked indexed agenda, category, season, and individual event pages to verify
  that concerts exist. Results expose categories such as `Musique`,
  `Orchestre Symphonique`, and `Conservatoire en scène`, but the live pages
  cannot be fetched to validate exact filter identifiers, pagination behavior,
  adjacent-category coverage, contamination, or detail-page parsing.

## What would unblock implementation

Allowlisting the crawler-factory egress traffic in MalCare, removing the false
positive firewall rule, or providing an accessible first-party API/feed mirror
would allow the site taxonomy, pagination, archives, and event detail pages to
be investigated and a crawler implemented. The source should then be reassessed
for comprehensive first-party filters before choosing `classical`; without a
verified comprehensive filter, the safe upload target would be `potential`.
