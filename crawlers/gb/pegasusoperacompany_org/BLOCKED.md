<!-- crawler-factory-metadata
{"url":"https://pegasusoperacompany.org/","geographic_scope":"country","country_code":"GB","reason_code":"access_blocked","attempted_at":"2026-08-19","retry_after":"2026-09-18"}
-->

# Crawler blocked by SiteGround bot protection

## Original URL

https://pegasusoperacompany.org/

Pegasus Opera Company is a London-based UK opera company, so the resolved
geography is country scope with ISO country code `GB`.

## Why a crawler cannot currently be implemented

All production-compatible HTTP requests return a SiteGround robot challenge
with status `202` instead of the requested page or API response. The challenge
requires JavaScript and issues an opaque, HTTP-only `_I_` clearance cookie.
Python `requests`, including a session with a normal browser user agent, cannot
obtain this cookie, so a `BaseCrawler` implementation would consistently parse
an empty challenge document rather than concerts.

An interactive Playwright browser eventually passed the challenge and confirmed
that the source still exposes current events and past shows. Hard-coding the
browser's clearance cookie would be temporary, environment-specific, and unsafe,
and the repository's crawler runtime does not expose a supported browser-based
fetch interface.

## Approaches attempted

- Inspected the homepage and `/whats-on/` with Playwright and reviewed their
  network requests. No first-party event API, genre/category filter, or
  pagination API was called; the listings are rendered in the returned HTML.
- Inspected the first-party WordPress REST API at `/wp-json/wp/v2/types` and
  `/wp-json/wp/v2/search`. WordPress exposes ordinary posts and pages, not a
  dedicated event type or structured event feed, and direct HTTP access to the
  REST endpoints is protected by the same challenge.
- Inspected `/sitemap_index.xml`, `/wp-sitemap.xml`, and `robots.txt`. These are
  also challenge-protected for ordinary HTTP clients.
- Inspected `/whats-on/` after browser clearance. It contained concrete Opera
  Gala pages alongside choir recruitment and recurring community-choir pages;
  no applicable first-party filter controls or stable filter values were
  exposed.
- Inspected `/our-work/past-shows/` and triggered its infinite scroll. The
  browser requested the stable HTML route
  `/our-work/past-shows/page/2/`, confirming HTML pagination rather than an API.
  Direct requests to that page and representative current event detail pages
  still returned only the `202` challenge.
- Retried direct requests with standard Chrome, Googlebot, Bingbot, and curl
  user agents. All received the same challenge response.

Because the unfiltered current listing mixes concrete classical performances
with recruitment and recurring participation pages, a future crawler should use
`upload_target="potential"` unless it can establish a reliable detail-level
rule or the site adds comprehensive first-party event filters.

## What would unblock implementation

Any one of the following would make a reliable crawler possible:

- SiteGround allow-listing the crawler runtime or disabling the JavaScript
  challenge for public listings, archive pagination, detail pages, and the
  WordPress REST API.
- A stable first-party event API or feed that is accessible without browser
  clearance and includes dates, venues, and locations.
- A supported browser-fetch facility in the production crawler runtime that can
  complete and maintain the SiteGround clearance flow.

