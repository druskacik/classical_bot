<!-- crawler-factory-metadata
{"url":"https://www.seattlesymphony.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-19","retry_after":"2026-09-18"}
-->

# Seattle Symphony crawler blocked

## Original URL

https://www.seattlesymphony.org/

Seattle Symphony is a United States organization based in Seattle, Washington, so the resolved country code is `US`.

## Why implementation is currently blocked

The site is protected by Azure Web Application Firewall. Requests from the crawler environment receive HTTP 403 responses containing an Azure WAF JavaScript challenge or a `Service unavailable` page whose body says `The request is blocked.` This also affects event listings, individual event details, query-filtered listings, pagination, and static first-party PDF assets.

Current concert pages are indexed by public search engines, so the source is not an empty or repurposed site. However, search results and cached snippets are neither a complete nor stable first-party feed and cannot support a reliable production crawler.

## Approaches attempted

### Browser and network inspection

- Loaded the homepage and `/en/concerttickets/calendar` in headless Chromium with Playwright while recording document, XHR, and fetch responses.
- Both navigations ended at HTTP 403 `Service unavailable` pages before any application JavaScript or event-data request could run.
- No API, GraphQL, XHR, or fetch endpoint was exposed in the network log because the WAF blocked the initial documents.

### API and filtered-feed investigation

- Checked the canonical event listing at `/concerttickets/event-calendar` and a `page=2` pagination request; both returned HTTP 403.
- Tested first-party filter query shapes visible in indexed historical calendar URLs: `filters=Octave 9&useOnlyVisibleEventCategories=true` and `filters=2026-2027 Symphonic&useOnlyVisibleEventCategories=true`; both returned HTTP 403.
- Tested a representative current detail URL, `/en/concerttickets/calendar/2026-2027/26fam4`; it also returned HTTP 403.
- Public indexing shows source series/categories including Symphonic, Seattle Pops, In Recital, Chamber, Octave 9, Family Concerts, Tiny Tots, and Specials. Because the live listing and its network traffic are inaccessible, their exact current identifiers, pagination persistence, date-range behavior, completeness, and contamination cannot be verified safely.

### HTML and asset parsing

- Plain HTTP requests to the homepage, event calendar, filtered calendars, detail page, `robots.txt`, and a first-party season brochure all received the same WAF challenge/block response.
- The returned HTML contains only Azure WAF challenge markup and no concert cards, structured data, or application bundle URLs that could be parsed.

## What would unblock implementation

Any one of the following would permit a reliable crawler:

- allowlisting the production crawler egress IP or relaxing the Azure WAF rule for read-only public calendar and detail requests;
- a documented or discoverable public JSON, RSS, ICS, or other first-party event feed that is reachable without the blocked site frontend;
- access to a browser/network environment accepted by the WAF, so the calendar API and stable filter identifiers can be inspected and then tested from the production crawler environment.

Once access is available, the calendar should be re-investigated network-first. The source appears to mix Seattle Symphony classical/crossover events with broader Benaroya Hall programming, so the upload target must remain undecided until first-party categories and representative adjacent filters can be verified. If a comprehensive in-scope filtered feed cannot be proven, the correct target will be `potential`.
