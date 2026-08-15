<!-- crawler-factory-metadata
{"url":"https://www.gso.se/","geographic_scope":"country","country_code":"SE","reason_code":"access_blocked","attempted_at":"2026-08-15","retry_after":"2026-09-14"}
-->

# Access blocked

## Original URL

https://www.gso.se/

The source is Göteborgs Symfoniker / Göteborgs Konserthus, a Swedish organization whose programme includes home performances in Göteborg and explicitly identified tour performances elsewhere. Its resolved geographic scope is Sweden (`SE`), not multi-country.

## Why a crawler cannot currently be implemented

The hostname resolves to `217.13.235.56`, but the site accepts neither browser nor direct HTTPS connections from the crawler-factory environment. Playwright navigation timed out after 60 seconds before `DOMContentLoaded`, and subsequent Playwright network inspection also timed out. Direct IPv4 HTTPS requests to both `https://www.gso.se/` and `https://gso.se/` timed out without receiving an HTTP status line or response headers. Because no live response can be fetched, a production parser cannot be implemented and validated safely against the current markup or network protocol.

Search-engine-indexed copies confirm that the site currently publishes concrete concerts, so this is not a `no_current_events` case. Those copies are not a stable first-party endpoint usable by a production crawler.

## Approaches attempted

- **Playwright/network:** Navigated to the canonical homepage with Playwright first. The navigation timed out before any page content loaded. Attempting to read the captured network-request list also timed out, so no API request or response body could be reconstructed.
- **Direct HTTP/API discovery:** Resolved the canonical host and attempted HTTPS with a browser user agent, forced IPv4, and both `www` and apex hostnames. Every attempt timed out before an HTTP response. Consequently WordPress or other API endpoints could not be enumerated from live scripts or network traffic.
- **HTML:** Tried the homepage and known first-party programme paths, including `/program/` and `/en/programme/`; no HTML was returned to this environment.
- **Indexed first-party evidence:** Inspected indexed versions of `/en/programme/concerts/?show_dates=1`, representative concert detail pages, and pagination (`offset=60`). The calendar is a mixed venue feed and exposes first-party genre values including Children/Families `32`, Classical `29`, Organ `281`, and Gothenburg Symphony `337`, alongside Christmas concert, Classical for beginners, Jazz/World, Lecture, Pop/Rock, School, and Show/Stand up. The unfiltered feed demonstrably contains nonclassical concerts, talks, guided tours, and other non-events. Although the visible `offset=60` pagination preserves `show_dates=1`, live access was unavailable to verify that single or combined `concert_genre[]` identifiers persist across current pagination/date ranges, to inspect all relevant category identifiers, or to check coverage and contamination across representative current detail pages.

## What would unblock implementation

Restore HTTPS reachability from the crawler-factory/production egress network, or provide a stable first-party API/feed reachable from that network. A retry should capture Playwright network traffic, verify all scope-relevant category IDs (not only Classical), test combined filters across `offset` pagination and explicit past/future date ranges, and inspect representative adjacent-category details. If a comprehensive filtered feed cannot be proven clean, the reachable mixed calendar should be scraped with `upload_target="potential"`.
