<!-- crawler-factory-metadata
{"url":"https://www.mosaic-festival.com/","geographic_scope":"multi_country","country_code":null,"reason_code":"access_blocked","attempted_at":"2026-08-14","retry_after":"2026-09-13"}
-->

# Mosaic Seasons crawler blocked

## Original URL

https://www.mosaic-festival.com/

## Why a crawler cannot currently be implemented

The live website is protected by a SiteGround robot challenge. Every attempted
first-party route returns HTTP 202 with a redirect to
`/.well-known/sgcaptcha/`, so a production crawler cannot enumerate concert
pages or retrieve their required dates, venues, cities, and descriptions.

Search-engine copies show that Mosaic Seasons is a classical music festival
which presents events in several countries. They also show historical gallery
dates and general programme announcements, but those overview pages are not a
complete, structured list of concrete performances and cannot support a
universal crawler.

## Approaches attempted

- Opened the homepage in Playwright with browser cookies enabled and inspected
  its network traffic. The initial request was replaced by the SiteGround
  `sgcaptcha` challenge; no concert API or application-data request was made.
- Requested WordPress discovery and API routes, including `/wp-json/`, REST
  page/post/search endpoints, `?rest_route=/wp/v2/pages`, RSS, `robots.txt`,
  `wp-sitemap.xml`, and a page sitemap. The challenge intercepted these routes
  too.
- Tried the canonical host with and without `www`, over HTTPS and HTTP, and
  tested individual indexed pages such as `/tickets/`. The same protection was
  applied consistently.
- Inspected search-indexed first-party pages and historical URL listings. They
  expose festival overviews, artist profiles, gallery material, and isolated
  announcements, but no complete parseable calendar with valid occurrence
  dates and locations.
- Looked for first-party genre, category, discipline, event-type, series, and
  tag filters. No applicable event-feed filters or stable pagination parameters
  were exposed. The available indexed text describes the source itself as a
  classical music festival.

## What would unblock implementation

Any stable first-party endpoint accessible without the robot challenge would
unblock another attempt: for example, an allowlisted WordPress REST API,
sitemap/RSS feed, public calendar export, or server-rendered programme pages
containing every concrete occurrence. Access from the production crawler IP
could also work if the site owner exempts it from the SiteGround challenge.
