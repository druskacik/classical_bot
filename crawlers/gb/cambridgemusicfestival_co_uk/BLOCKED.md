<!-- crawler-factory-metadata
{"url":"https://cambridgemusicfestival.co.uk/","geographic_scope":"country","country_code":"GB","reason_code":"access_blocked","attempted_at":"2026-08-18","retry_after":"2026-09-17"}
-->

# Cambridge Music Festival crawler blocked

Original URL: https://cambridgemusicfestival.co.uk/

Cambridge Music Festival is a Cambridge, United Kingdom classical-music promoter. Its indexed first-party pages show concrete performances and an event archive, but the live site does not currently expose scrapeable content to this environment. Every tested route returns an HTTP 202 SiteGround CDN robot challenge (`sg-captcha: challenge`) instead of the requested resource, and the interactive browser remains in the CAPTCHA flow.

## Approaches attempted

- Loaded the home page and `/whats-on/` with Playwright and inspected all network requests. Only SiteGround CAPTCHA and challenge-asset requests were exposed; no event API request was made.
- Waited for the browser challenge to resolve. It progressed from `/.well-known/sgcaptcha/` to `/.well-known/captcha/` and requested a bot-detection image, but never returned the website.
- Tested likely WordPress discovery and structured-data routes: `/wp-json/`, `/wp-json/wp/v2/types`, `/wp-json/wp/v2/event?per_page=1`, and `/wp-sitemap.xml`. All were intercepted by the same challenge.
- Tested HTML and discovery routes including `/`, `/whats-on/`, and `/robots.txt` over HTTPS, plus the HTTP origin. All were intercepted.
- Confirmed via indexed first-party results that `/whats-on/`, `/event-category/events/`, and individual `/event/.../` pages exist and contain dates, times, venues, and long programme descriptions. These cached search representations are not a stable first-party feed suitable for a production crawler.

## Scope and filters

The source describes itself as a classical-music promoter and its event pages form a classical-only programme, so a working crawler would use `upload_target="classical"`. The only applicable first-party taxonomy value visible from indexed URLs was the event category `events` at `/event-category/events/`. No genre, discipline, series, or tag filters could be inspected live because the challenge blocks both HTML and API discovery. Pagination persistence and archive coverage therefore could not be verified.

## What would unblock implementation

Implementation can proceed when the SiteGround challenge permits non-interactive requests from the crawler environment, or when the publisher supplies/allowlists a stable event API, RSS/ICS feed, sitemap, or HTML endpoint. At that point, inspect the WordPress REST types and network traffic first, verify archive pagination, and fall back to parsing the `/event/` detail pages if no structured feed is available.
