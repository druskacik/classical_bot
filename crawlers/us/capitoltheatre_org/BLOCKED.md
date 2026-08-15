<!-- crawler-factory-metadata
{"url":"https://capitoltheatre.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-15","retry_after":"2026-09-14"}
-->

# Capitol Theatre crawler blocked

## Original URL

https://capitoltheatre.org/

This is the website of The Capitol Theatre in Yakima, Washington, United States.
It is a mixed performing-arts venue whose calendar includes classical and pops
orchestra concerts, ballet, musicals, popular music, comedy, talks, tours, and
other non-concert events.

## Why a crawler cannot currently be implemented

Cloudflare returns an HTTP 403 challenge page for every first-party page and
machine-readable endpoint tested. The challenge does not resolve in the
Playwright browser, and ordinary production-style HTTP requests receive the
same response. A crawler built against the visible HTML would therefore fail
before it could discover any events.

Search-engine results confirm that the site currently publishes both upcoming
and archived concrete events under `/events/` and `/events/archive/`, with
details at routes such as `/events/detail.html?calendarid=608`. Search results
are not a stable or first-party data source and cannot be used as the production
crawler transport.

## Approaches attempted

- Opened the home page with Playwright and inspected its network requests. The
  only dynamic traffic exposed was Cloudflare challenge/Turnstile traffic; no
  event API request was made before access was denied.
- Waited for the browser challenge to resolve, but the page remained an HTTP
  403 `Just a moment...` page.
- Requested the home page, `www` host, HTTP redirect, `/events/`, `robots.txt`,
  `sitemap.xml`, and `/wp-json/` using production-style HTTP requests. All
  first-party requests were blocked by the same HTTP 403 challenge.
- Probed the first-party ticketing host and likely public Spektrix API paths.
  The guessed API routes returned HTTP 404 and did not expose the venue's event
  catalogue.
- Inspected indexed upcoming, archive, series, and representative detail pages.
  These establish that events and long programme descriptions exist, but do
  not provide a reliable way to fetch the source in production.

The calendar UI exposes first-party `Category` and `Genre` selectors, plus
series pages for Capitol Best, Capitol Kids, Yakima Town Hall, YSO Capitol,
YSO Classical, and YSO Pops. Exact selector values and their persistence across
pagination/date ranges could not be tested because Cloudflare blocks the actual
page and its scripts. Indexed evidence also shows that the full calendar is
mixed. Therefore an eventual crawler should use `upload_target="potential"`
unless stable first-party filters can be accessed and verified to cover all
eligible orchestra, ballet, crossover, and qualifying musical events without
contamination.

## What would unblock implementation

Any of the following would allow a working crawler to be built:

- allowlisting the crawler runtime or otherwise removing the Cloudflare
  challenge for read-only calendar and detail requests;
- documentation or a confirmed public event API endpoint and required stable
  parameters/identifiers; or
- a first-party export/feed (JSON, XML, iCalendar, or equivalent) reachable
  without an interactive challenge and covering upcoming and archived events.

