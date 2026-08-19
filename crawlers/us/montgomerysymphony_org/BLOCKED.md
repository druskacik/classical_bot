<!-- crawler-factory-metadata
{"url":"https://www.montgomerysymphony.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-19","retry_after":"2026-09-18"}
-->

# Montgomery Symphony Orchestra crawler blocked

## Original URL

https://www.montgomerysymphony.org/

## Why a crawler cannot currently be implemented

The source is the US-based Montgomery Symphony Orchestra in Montgomery, Alabama. It publishes concrete classical concerts, including current events and archived season material, but the origin currently returns a SiteGround JavaScript robot challenge instead of source content to automated clients. The challenge is applied to both public HTML pages and machine-readable WordPress endpoints. A production crawler using the repository's HTTP-based interfaces would therefore receive challenge HTML rather than concert data.

Search-engine results confirm that concert material exists (including the 2025-2026 season, Fellowship Recitals, Montgomery Youth Orchestra concerts, and individual Masterworks and Jubilee Pops pages), so this is not an empty-calendar result. Search-engine excerpts are not a stable or first-party scrape endpoint and cannot support a production crawler.

## Approaches attempted

- Requested the homepage and concert pages as browser-like HTTP traffic. The server returned HTTP 202 with a redirect to `/.well-known/sgcaptcha/` rather than page HTML.
- Tested the WordPress REST discovery endpoint and page/search APIs at `/wp-json/`, `/wp-json/wp/v2/pages`, `/wp-json/wp/v2/search`, and the `?rest_route=/wp/v2/pages` variant. Every endpoint returned the same challenge response.
- Tested WordPress sitemap discovery at `/wp-sitemap.xml` and `robots.txt`. Both were challenge-protected.
- Loaded the challenge with headless Chromium and allowed its JavaScript to run. It did not resolve to the requested site content in this environment.
- Reviewed indexed first-party pages and representative detail-page excerpts to confirm that the organization publishes qualifying orchestral, recital, film-with-orchestra, pops, and youth-orchestra performances. The site exposes season/series navigation, but no verified genre/category API filter or stable paginated event feed could be inspected through the access barrier.

## What would unblock implementation

Any stable first-party access path that works without the SiteGround challenge would unblock the crawler, for example allowlisting the crawler's production egress, disabling the challenge for public WordPress REST or sitemap routes, or providing a public calendar/feed API. Once accessible, the season pages, fellowship recital page, youth-orchestra page, and individual concert detail pages should be checked together so archived and current qualifying performances are covered without treating season overview pages as events.
