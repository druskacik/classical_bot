<!-- crawler-factory-metadata
{"url":"https://www.foliasmusic.com/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-18","retry_after":"2026-09-17"}
-->

# Folias Duo crawler blocked by robot challenge

## Original URL

https://www.foliasmusic.com/

Folias Duo is based in Grand Rapids, Michigan, so the resolved geographic scope is the United States. The organization tours internationally, but that does not make the source itself multi-country.

## Why a crawler cannot currently be implemented

Every tested first-party route returns a SiteGround robot-challenge response instead of the requested content. The challenge advances to an image CAPTCHA that requires cookies and human interaction. A production crawler cannot solve or bypass that CAPTCHA reliably, and treating the challenge HTML as a listing would yield no concerts.

Search-engine indexing confirms that the domain belongs to Folias Duo, a flute-and-guitar composer-performer ensemble, and that concrete Folias Duo concerts exist on venue and third-party calendar sites. However, those external listings are not a universal scrapeable feed published by the assigned website. They also do not establish a stable first-party API, archive, pagination scheme, or complete tour calendar for this crawler.

## Approaches attempted

- Opened the canonical homepage with Playwright and inspected its network requests. Navigation was redirected from `/` through `/.well-known/sgcaptcha/` to `/.well-known/captcha/`; only the challenge resources were requested, with no concert API or listing request available to reconstruct.
- Inspected the rendered challenge with Playwright. It requires retyping an image CAPTCHA and explicitly requires browser cookies.
- Requested the canonical homepage and common discovery/API routes with an ordinary HTTP session and browser-like user agent: `/robots.txt`, `/sitemap.xml`, and `/wp-json/`. Each returned HTTP 202 challenge HTML rather than robots data, a sitemap, or WordPress JSON.
- Tested the bare-host variant `https://foliasmusic.com/` and the HTTP canonical-host variant. Both returned the same challenge flow.
- Searched the indexed first-party pages for concerts, events, tour dates, filters, categories, tags, pagination, and API evidence. Indexed pages expose biography, records, school, contact, blog, and travel-category content, including historical tour announcements, but no stable comprehensive event feed or applicable first-party event filter could be verified.
- Inspected representative indexed adjacent material. It confirms that the ensemble blends classical music with jazz, world music, and improvisation, so any broad or reconstructed candidate feed would require careful inclusion handling. No exact first-party filter values could be tested, and persistence across pagination or date ranges could not be verified because the source is blocked.

## What would unblock implementation

Any of the following would permit a reliable crawler investigation and implementation:

- allowlisting the crawler's production egress IP or disabling the SiteGround challenge for public listing/API routes;
- a documented, unauthenticated first-party tour/event API or calendar feed (JSON, RSS, XML, or iCalendar) that is exempt from the challenge;
- server-rendered event listing and detail pages accessible without CAPTCHA; or
- a stable first-party embedded calendar endpoint whose complete configuration and pagination/date-range behavior can be inspected from the accessible site.

Once access is available, the investigation must verify all relevant event categories and adjacent categories against the project inclusion guidance before selecting `classical` versus `potential` upload.
