<!-- crawler-factory-metadata
{"url":"https://sinfonicaabruzzese.eu/","geographic_scope":"country","country_code":"IT","reason_code":"access_blocked","attempted_at":"2026-08-12","retry_after":"2026-09-11"}
-->

# Access blocked

## Original URL

https://sinfonicaabruzzese.eu/

The Istituzione Sinfonica Abruzzese is an Italian orchestra. Its calendar also
contains touring performances, but that does not make the source multi-country;
the resolved crawler geography is therefore Italy (`IT`). Search-indexed pages
confirm that the site publishes concrete concerts and retains past concerts at
`/archivio-concerti/`.

## Why a crawler cannot currently be implemented

SiteGround's robot challenge intercepts every request from the crawler
environment. It returns HTTP 202 and a small HTML meta-refresh page leading to
`/.well-known/sgcaptcha/`, rather than the requested calendar, event detail,
feed, sitemap, or API response. The challenge did not clear in a cookie-enabled
Playwright browser. A production crawler would consequently parse only the
challenge page and could not return validated event records.

## Approaches attempted

- Opened the canonical homepage with Playwright and inspected its network
  requests. Navigation was redirected through `/.well-known/sgcaptcha/` and
  `/.well-known/captcha/`; no site application or event API requests were made.
- Waited for the browser challenge to complete with cookies enabled. It remained
  on the Robot Challenge Screen.
- Requested the homepage, `/prossimi-concerti/`, `/archivio-concerti/`, a known
  `/eventi/.../` detail-page shape, `robots.txt`, `sitemap_index.xml`,
  `wp-sitemap.xml`, `/feed/`, and `/eventi/feed/`. All first-party requests were
  intercepted by the same challenge.
- Tested WordPress REST discovery and likely event endpoints through both
  `/wp-json/...` and `?rest_route=...`, including `/wp-json/wp/v2/types`,
  `/wp-json/wp/v2/search`, and `/wp-json/wp/v2/etn`. The `www` hostname and the
  organization's alternate `.it` domain were also tested; all were intercepted.
- Search-indexed copies were inspected only to establish that concerts and an
  archive exist and that event pages expose date, time, venue, category, and
  programme text. Search results are not a stable first-party scrape source and
  cannot support a production crawler.

## Filters and upload-target assessment

Search-indexed event details expose the first-party category value `Concerti`,
and the upcoming and archive pages appear to be concert-specific feeds. However,
the category endpoint and pagination could not be reached, so the stable filter
identifier, its persistence across pagination/date ranges, adjacent categories,
and complete coverage could not be verified. No upload target is selected
because no working crawler can be safely implemented. If access is restored and
the `Concerti` feed is verified as the orchestra's complete concrete performance
feed, the classical-only institutional source would support `classical`;
otherwise it should use `potential` until classification confirms coverage.

## What would unblock implementation

Allowlisting the crawler environment, disabling the SiteGround challenge for
public read-only calendar/API routes, or providing a stable first-party JSON,
RSS, iCalendar, or HTML endpoint that is not challenge-protected would unblock
implementation. The endpoint must permit pagination through both upcoming and
retained past concerts and access to detail-page programme text.
