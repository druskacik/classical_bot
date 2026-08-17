<!-- crawler-factory-metadata
{"url":"https://www.staatstheater-nuernberg.de/","geographic_scope":"country","country_code":"DE","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Access blocked

## Original URL

https://www.staatstheater-nuernberg.de/

The source is the Staatstheater Nürnberg in Germany, so the resolved country code is `DE`.

## Why a crawler cannot currently be implemented

The site's origin (`213.95.102.12`) accepted neither browser nor direct HTTPS connections during this attempt. Every connection timed out before an HTTP response was received. The same failure affected the site's alternate first-party hostname without the hyphen, which resolves to the same address. An independent indexed-page fetch subsequently returned `502 Bad Gateway` for both season calendars.

Search indexing confirms that the site does publish concrete current and archived performances, but cached search text is not a stable or complete scrapeable source. Without a live response it is not possible to verify an API contract, pagination/date coverage, exact first-party filter identifiers, event links, detail descriptions, or location parsing against representative pages.

## Approaches attempted

- Playwright navigation to the original URL timed out after 60 seconds while waiting for the initial document. The Playwright request log and browser close operation then also timed out, so no network response or API request could be inspected.
- DNS resolution succeeded for `www.staatstheater-nuernberg.de` and `www.staatstheaternuernberg.de`; both resolve to `213.95.102.12`.
- Direct HTTPS requests to the home page, `/spielplan/`, `/spielplan-25-26`, and `/spielplan-26-27` timed out before response headers.
- Indexed first-party pages were inspected to establish that season calendars and concrete event occurrences exist. Their filter UI exposes divisions including `Oper`, `Schauspiel`, `Ballett`, `Konzert`, and `PLUS`; categories including `Oper`, `Operette`, `Musical`, `Philharmonische Konzerte`, `Kammerkonzerte`, `Kinderoper`, and `Kinder- und Jugendkonzerte`; and event types including `Konzert`, `Matinée`, `Premiere`, `Soirée`, `Wiederaufnahme`, and `Öffentliche Probe`.
- Indexed query URLs show the parameter names `sparte`, `genre`, `activityCategory`, `location`, `date`, and `search`, but the exact option values and their behavior across pagination/date ranges could not be tested. No API endpoint could be reconstructed because the origin never returned the document or JavaScript/network traffic.
- HTML parsing could not be evaluated because no live HTML or detail page was obtainable. Search-result excerpts were deliberately not used as crawler input.

## What would unblock implementation

Restore network access to the site origin (or provide a reachable first-party mirror/API). A retry can then inspect the calendar's network requests, verify stable filter identifiers and season/date traversal, compare the broad `Oper`, `Ballett`, and `Konzert` coverage with adjacent categories, inspect representative event detail pages, and implement and test a `BaseCrawler` parser.
