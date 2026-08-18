<!-- crawler-factory-metadata
{"url":"https://www.bouldersymphony.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-18","retry_after":"2026-09-17"}
-->

# Boulder Symphony crawler blocked

## Original URL

https://www.bouldersymphony.org/

## Why implementation is currently blocked

The site rejects this crawler environment at its access-control edge. Every
first-party route tested returned HTTP 403 with an `Access Denied` response,
before event HTML or structured data was delivered. A production crawler built
against search-engine excerpts or guessed WordPress markup would therefore not
be testable or reliable.

The organization is based in Boulder, Colorado, and the resolved crawler
geography is the United States (`US`). Search-index evidence shows that the site
does publish both current concerts and an events archive, so this is not an
empty-calendar case.

## Approaches attempted

- Loaded the home page and WordPress REST category endpoint with Playwright and
  inspected network requests. Navigation itself received HTTP 403, leaving no
  event API/XHR traffic to reconstruct.
- Tested the first-party `/events/` listing and the paginated
  `/category/events/page/2/` archive as HTML sources; both returned HTTP 403.
- Tested likely WordPress structured sources: `/wp-json/`,
  `/wp-json/wp/v2/categories?search=Events`, and `/wp-json/wp/v2/posts`; all
  returned HTTP 403 rather than JSON.
- Tested `/feed/`, `/category/events/feed/`, `/wp-sitemap.xml`, and
  `/robots.txt`; all were also denied with HTTP 403.
- Tested both `www.bouldersymphony.org` and the canonical non-`www` host with
  browser-like and crawler user agents. The result did not change.

The visible first-party taxonomy in search-indexed pages separates `Symphony
Events` from `Academy Events`, and the WordPress archive uses the `Events`
category. Because the server denied all live responses, exact category IDs,
pagination stability, detail-page fields, and adjacent-filter contamination
could not be verified safely.

## What would unblock implementation

Allowlisting the production crawler egress address/user agent, relaxing the
site firewall for public event and WordPress feed/API routes, or providing a
documented first-party event feed would make implementation possible. Once
access is restored, the `/events/` listing and `Events` archive/API should be
re-investigated for stable pagination, past-event coverage, detail-page venue
and date data, and separation of Symphony events from academy classes.
