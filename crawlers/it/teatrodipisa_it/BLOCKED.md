<!-- crawler-factory-metadata
{"url":"https://teatrodipisa.it/","geographic_scope":"country","country_code":"IT","reason_code":"access_blocked","attempted_at":"2026-08-12","retry_after":"2026-09-11"}
-->

# Access blocked by anti-bot challenge

Original URL: https://teatrodipisa.it/

The source is the Pisa-based Fondazione Teatro di Pisa, so the resolved country is Italy (`IT`). The site still publishes concrete current and archived performances, but all live requests from the crawler environment are intercepted by a SiteGround robot challenge. The challenge returns HTTP 202 and a small HTML redirect/check page instead of the requested event data. A crawler cannot currently obtain or validate dates, detail text, venues, or pagination reliably.

## Approaches attempted

- Opened the homepage with Playwright and inspected its network traffic. Navigation was redirected to `/.well-known/sgcaptcha/`, and the only subsequent requests were challenge assets served from CloudFront; no event API request was made by the application.
- Navigated directly with Playwright to `/wp-json/`, `/wp-sitemap.xml`, and `/robots.txt`. Each endpoint returned the same HTTP 202 connection-security challenge rather than JSON, XML, or text.
- Tested the likely The Events Calendar REST API at `/wp-json/tribe/events/v1/events?per_page=3` and its `?rest_route=/tribe/events/v1/events&per_page=3` equivalent. Both were challenged before any structured response was returned.
- Tested the first-party filtered archive `/calendario/categoria/concerti/elenco/?eventDisplay=past`; it was also challenged. Indexed first-party routes show category values `concerti` and `opera`, with monthly and past views, but live pagination and date-range behavior could not be verified. Search-index evidence also shows that the overall source is mixed (including prosa, events, dance, and other nonclassical programming), so an unfiltered feed could not safely be uploaded as classical.
- Tried the historical hostname `https://www.teatrodipisa.pi.it/`; it redirects to the same challenged canonical domain.

## What would unblock implementation

Allowlisting the crawler's production egress IP/user agent, disabling the challenge for public calendar and REST endpoints, or providing an authenticated/public event feed would unblock the crawler. Once access is restored, the `concerti` and `opera` filters should be tested across REST/list pagination and past/future date ranges, alongside adjacent `danza` and family-programming categories, before deciding whether a comprehensive filtered feed can upload directly as `classical` or must use `potential`.
