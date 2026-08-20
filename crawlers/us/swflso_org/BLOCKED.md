<!-- crawler-factory-metadata
{"url":"https://www.swflso.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# Southwest Florida Symphony crawler blocked

## Original URL

https://www.swflso.org/

## Why a crawler cannot currently be implemented

The source is protected by a Cloudflare Turnstile challenge that returns HTTP 403 to both an interactive Playwright browser and ordinary HTTP clients. The protection also covers the site's discovery and WordPress API endpoints, so there is no stable first-party event feed or HTML catalogue that a production crawler can currently retrieve.

The Southwest Florida Symphony announced that it would permanently close on June 30, 2025. Although this means there are no expected current concerts, an archive crawler would still be appropriate if past concert pages remained accessible. The access challenge prevents confirming or scraping any such archive.

## Approaches attempted

- Loaded `https://www.swflso.org/` in Playwright and inspected its network traffic. The document returned HTTP 403; the only dynamic requests were Cloudflare challenge/Turnstile requests, with no concert API request exposed.
- Waited for the browser challenge, but it did not resolve into site content.
- Tested first-party WordPress discovery routes in Playwright: `/wp-json/`, `/wp-sitemap.xml`, and `/robots.txt`. Each returned the same HTTP 403 challenge.
- Tested `www` and non-`www` hostnames over HTTPS, the HTTP URL, `/wp-json/wp/v2/`, and `/wp-sitemap-posts-page-1.xml` with a normal HTTP client. Every request returned HTTP 403 challenge HTML.
- Searched for indexed first-party concert/event URLs. Results exposed the organization's closure announcement and uploaded documents, but no accessible, complete event archive or stable first-party event/category feed suitable for a universal crawler.

No applicable first-party genre, category, discipline, event-type, series, or tag filters could be inspected because no event feed or catalogue was accessible. Pagination and date-range persistence therefore could not be tested.

## What would unblock implementation

Any of the following would permit another implementation attempt:

- allowlisted non-interactive access to the site's event pages, sitemap, or WordPress REST API;
- a stable, publicly accessible first-party events API/feed that is not covered by the challenge; or
- removal or reconfiguration of the Cloudflare challenge so archived concert pages can be retrieved without an interactive CAPTCHA.

