<!-- crawler-factory-metadata
{"url":"https://thesymphony.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Crawler blocked by origin access controls

## Original URL

https://thesymphony.org/

The source is the Santa Barbara Symphony in Santa Barbara, California, so the
resolved geographic scope is the United States (`US`). The website has current
and archived scrapeable concert information, but its origin does not presently
allow this crawler environment to retrieve it reliably.

## Why implementation is blocked

Every first-party request tested returned an HTTP 403 Cloudflare challenge page.
This includes a real browser session and ordinary HTTP requests. A production
crawler based on the challenge response would return no concerts, and search
engine copies are neither a stable nor a first-party scraping interface.

## Approaches attempted

- Browser/network investigation with Playwright against the home page. The only
  observed dynamic traffic was Cloudflare challenge-platform/Turnstile traffic;
  no concert API request or structured event response became available.
- Direct HTML requests to the home page, the current Orchestra Concerts page,
  the Past Concerts page, and the Youth Ensembles Concerts page.
- WordPress discovery and API variants: `/wp-json/`,
  `/wp-json/wp/v2/types`, `/wp-json/wp/v2/pages`, the `?rest_route=` form,
  `/wp-sitemap.xml`, `/feed/`, and `/robots.txt`.
- Alternate protocol and hostname forms (`http://thesymphony.org/` and
  `https://www.thesymphony.org/`).

All origin approaches returned the same 403 access challenge. Publicly indexed
copies confirm that the site exposes separate first-party pages for current
orchestra concerts, past concerts, and youth ensemble concerts, with concrete
dates, times, titles, repertoire, and detail links. No genre/category API or
stable pagination identifiers could be tested because the origin blocked all
application responses.

## What would unblock implementation

Allowlisting the crawler's production egress in Cloudflare, providing a stable
first-party API/feed that is exempt from the browser challenge, or otherwise
making the concert HTML accessible to non-interactive HTTP clients would allow
implementation. Once access is restored, investigation should cover all three
concert sections (current orchestra, archives, and youth ensembles), verify
their detail pages and pagination, and retain the classical upload target only
if those feeds remain limited to concrete performances by the Symphony and its
classical youth ensembles.
