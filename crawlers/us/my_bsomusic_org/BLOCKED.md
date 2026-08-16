<!-- crawler-factory-metadata
{"url":"https://my.bsomusic.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-16","retry_after":"2026-09-15"}
-->

# Crawler blocked

## Original URL

https://my.bsomusic.org/

This is the Baltimore Symphony Orchestra's United States event and ticketing
calendar. The resolved geography is therefore country-scoped to `US`.

## Why implementation is currently blocked

`my.bsomusic.org` is protected by an Imperva security check backed by hCaptcha.
Both the calendar (`/events`) and representative event pages (for example,
`/overview/20347` and `/20347/20424`) return the challenge document to automated
requests instead of event content. A production crawler cannot reliably or
appropriately solve that interactive challenge.

The separately accessible first-party WordPress site at `www.bsomusic.org`
publishes event overview posts, but those posts do not contain the required
occurrence-level data. For example, a post exposes only the range
`Jan 8–10, 2027`, while the protected Tessitura page supplies the three distinct
dates, start times, and the pairing of each occurrence with either Music Center
at Strathmore or Joseph Meyerhoff Symphony Hall. Inferring those pairings would
create invalid records, so the WordPress feed is not a sufficient fallback.

## Approaches attempted

- Loaded the home page and `/events` with Playwright and inspected network
  traffic before considering HTML parsing. The only dynamic requests exposed
  were Imperva and hCaptcha resources; no event API request was reachable.
- Followed the public BSO calendar redirect from
  `https://www.bsomusic.org/calendar` to `https://my.bsomusic.org/events`; it
  encountered the same challenge.
- Tested direct HTTP access to `/events`, `/overview/20347`, and an individual
  occurrence URL. Each returned the small Imperva challenge HTML rather than
  the calendar or event document.
- Inspected the first-party WordPress sitemap and REST API. The
  `bso-concert-events` endpoint reported 116 posts over two pages with
  `per_page=100`, so its pagination is stable, but the response omits exact
  performance dates, times, and venues.
- Inspected representative WordPress event HTML. It provides titles,
  descriptions, category/collection/series taxonomy terms, broad date ranges,
  and protected Tessitura overview IDs, but not enough data to create valid
  occurrence records.
- Verified from representative indexed pages that the protected calendar is a
  mixed feed. It exposes first-party collection choices including `Classical`,
  `Popular`, `Celebrations`, `Family Friendly`, `Education`, `Presenters`,
  `Family Collection`, `Strathmore`, `26-27 Season`, and `2026 Summer`.
  Keyword/query examples visible publicly included `k=Family_Collection` and
  `k=MusicBox`; because the live results and their pagination remained behind
  the challenge, filter persistence and comprehensive in-scope coverage could
  not be validated. The WordPress taxonomy feed also includes clearly
  nonclassical events, so it cannot safely be uploaded as classical.

## What would unblock implementation

Any stable, non-challenged first-party occurrence feed containing event ID,
date/time, and venue would unblock the crawler. Equivalent options are an
allowlisted crawler address, documented public Tessitura API access, or
server-rendered occurrence details on the accessible WordPress pages. Once
available, the mixed calendar's collection/series filters must be tested across
date ranges and pagination; if they cannot comprehensively isolate all eligible
classical, family, crossover, and related performances, the crawler should use
`upload_target="potential"`.
