<!-- crawler-factory-metadata
{"url":"https://liederalive.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-16","retry_after":"2026-09-15"}
-->

# Crawler blocked

## Original URL

https://liederalive.org/

## Why a crawler cannot currently be implemented

The site is protected by a Cloudflare interactive challenge. Requests for the homepage and all discovered catalogue/API candidates return HTTP 403 with a Turnstile “Just a moment...” page. A production crawler using the repository's HTTP interfaces would therefore receive challenge HTML rather than concert records.

The organization is based in San Francisco, California, so the resolved geographic scope remains the United States (`US`).

## Approaches attempted

- Browser/network investigation with Playwright: the homepage returned HTTP 403. The only non-static first-party requests were the document request and Cloudflare challenge submission; no concert or calendar API request was exposed.
- API investigation: WordPress REST endpoints at `/wp-json/` and `/wp-json/wp/v2/`, including a page search query, returned the same HTTP 403 challenge.
- Feed and sitemap investigation: `/feed/`, `/wp-sitemap.xml`, and the site-advertised `/sitemaps.xml` returned HTTP 403.
- HTML investigation: the homepage, `/events/`, and the `www` hostname returned challenge HTML instead of site content.
- `/robots.txt` was accessible and identified `/sitemaps.xml`, but that sitemap itself is challenge-protected and exposes no scrapeable event URLs.

No applicable first-party genre, category, discipline, event-type, series, or tag filters could be inspected because every content/API surface is blocked before application content loads.

## What would unblock implementation

Any stable, non-interactive first-party event source accessible to server-side HTTP clients would unblock the crawler—for example, allowlisting crawler traffic, relaxing the Cloudflare challenge for public event/API/feed/sitemap paths, or publishing an accessible calendar API or iCalendar feed.
