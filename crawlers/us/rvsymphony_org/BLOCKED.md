<!-- crawler-factory-metadata
{"url":"https://rvsymphony.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Crawler blocked by interactive robot challenge

## Original URL

https://rvsymphony.org/

This is the website of the Rogue Valley Symphony, an Oregon-based classical-music organization. Its indexed calendar and concert-detail pages show concrete performances in Ashland, Medford, and Grants Pass, Oregon, so the resolved country code is `US`.

## Why a crawler cannot currently be implemented

The live site redirects automated access to a SiteGround robot challenge at `/.well-known/sgcaptcha/` / `/.well-known/captcha/`. The challenge requires an interactive image or audio CAPTCHA and cookies. The interception occurs before the requested application route is served, returning HTTP 202 with HTML that redirects to the challenge. A production `requests` crawler therefore cannot obtain current listing or detail content, and it would be unsafe to build against stale search-engine snippets.

## Approaches attempted

- Opened the homepage in Playwright and inspected the resulting page and network activity. It redirected to the robot challenge; only challenge assets were requested, so no calendar API or application data request could be reconstructed.
- Requested likely WordPress structured endpoints in the Playwright browser context: `/wp-json/` and `/wp-json/wp/v2/types`. Both returned the same HTTP 202 challenge redirect instead of JSON.
- Requested likely discovery and HTML sources: `/wp-sitemap.xml` and `/robots.txt`. Both were intercepted by the same challenge before their contents were served.
- Inspected public search-index results for `/calendar/` and representative detail pages such as `/mw5/`, `/mw6/`, and `/summer/`. These confirm that the source publishes concrete classical performances and useful programme descriptions, but indexed snippets are neither a complete nor a current scrapeable source.

No applicable first-party genre, category, discipline, event-type, series, or tag filters could be tested because the challenge blocks the application and API before those interfaces load. The visible indexed calendar appears to be the symphony's own classical-only season feed, but pagination, archives, filter identifiers, coverage, and contamination could not be verified live.

## What would unblock implementation

Any stable first-party endpoint that is accessible without solving an interactive CAPTCHA would unblock the crawler, for example:

- allowlisting the crawler's production egress address;
- disabling the robot challenge for read-only calendar, sitemap, and WordPress REST routes;
- providing a public calendar feed or documented events API; or
- making the calendar and concert-detail HTML available to ordinary non-browser HTTP clients.

Once access is available, investigation should resume with the calendar's network requests and WordPress content types, verify pagination and archives, inspect representative concert pages, and then implement the crawler from the live first-party feed.
