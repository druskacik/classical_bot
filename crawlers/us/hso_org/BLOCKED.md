<!-- crawler-factory-metadata
{"url":"https://hso.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-18","retry_after":"2026-09-17"}
-->

# Huntsville Symphony Orchestra crawler blocked

## Original URL

https://hso.org/

The source is the Huntsville Symphony Orchestra in Huntsville, Alabama, so the resolved country code is `US` and the source is not multi-country.

## Why a crawler cannot currently be implemented

Every request to the canonical site is intercepted by a SiteGround robot challenge. The initial response has HTTP status 202 and redirects the client to `/.well-known/sgcaptcha/`; the challenge remains on a cookie-enabled Playwright browser and does not expose the event document. A production crawler using the repository's HTTP interfaces would therefore receive challenge HTML rather than concert data.

Search-engine results confirm that the site has concrete current concert listings, including dated 2026-2027 Huntsville Symphony Orchestra performances, so this is not an empty-calendar or wrong-source condition. Cached/indexed content is not a stable first-party scrape interface and cannot support a reliable crawler.

## Approaches attempted

- Opened `https://hso.org/` with Playwright and inspected its network traffic. Only the robot-challenge document and static challenge assets were available; no concert API request was issued.
- Requested the first-party event routes `/events/` and `/whatson/` while investigating current and legacy calendars. Access to the canonical host remained behind the same challenge.
- Probed common WordPress structured endpoints, including `/wp-json/`, `/wp-json/wp/v2/types`, `/wp-sitemap.xml`, and `/sitemap.xml`. All canonical-host requests returned the challenge instead of JSON or XML.
- Requested `/robots.txt` and the home page with a normal browser user agent and session cookies. These also returned challenge HTML.
- Investigated the HTML fallback through indexed first-party pages. Search results expose event summaries and indicate UI filters labelled `All`, `Concerts`, `Education`, and `Giving`, while an older/currently indexed calendar also shows `All`, `Concerts`, and `Education`. Because the live documents are blocked, the exact filter values, underlying stable query/API identifiers, pagination behavior, date-range persistence, detail-page markup, and archive coverage could not be verified.
- Checked representative indexed detail content. It shows concrete classical and crossover performances with dates and long programme descriptions, but indexed copies cannot be enumerated comprehensively or treated as a supported source feed.

No feed was selected and no upload target was chosen because neither a comprehensive classical feed nor a safe mixed candidate feed could be accessed and validated. In particular, selecting the visible `Concerts` label without testing adjacent education/family offerings would risk omitting in-scope events.

## What would unblock implementation

Any stable first-party interface that is accessible to the production crawler would unblock the work, for example:

- allowlisting the crawler/service egress address or user agent at SiteGround;
- disabling the challenge for read-only event, sitemap, or WordPress REST routes;
- providing the actual event API/feed URL and any required non-secret request parameters; or
- providing a server-rendered event calendar and detail pages that ordinary HTTP clients can access.

Once access is available, the `All`, `Concerts`, `Education`, and `Giving` filters must be mapped to their exact first-party identifiers, tested across pagination and date ranges, and compared on representative detail pages before deciding between a comprehensive filtered `classical` feed and an unfiltered/mixed `potential` feed.
