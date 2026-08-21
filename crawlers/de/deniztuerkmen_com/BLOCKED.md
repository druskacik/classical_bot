<!-- crawler-factory-metadata
{"url":"https://deniztuerkmen.com/","geographic_scope":"country","country_code":"DE","reason_code":"access_blocked","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# Crawler blocked

## Original URL

https://deniztuerkmen.com/

## Why a crawler cannot currently be implemented

The official site's schedule page does not publish concrete concert records. It only describes a 2026 European tour and links visitors to Deniz Türkmen's external Bandsintown artist page for dates, venues, cities, and tickets. The only dated items in the official page HTML are fully booked online masterclasses; these are not advertised as public performances and are outside the project's event scope.

The linked Bandsintown page returned HTTP 403 with a Cloudflare "Attention Required" response in both Playwright and a direct HTTP request. Consequently, no concert occurrence with the required date, venue, and city fields could be retrieved. The source is based in Germany; touring elsewhere does not make it a multi-country source for crawler metadata.

## Approaches attempted

- Loaded the home page and `/schedule` with Playwright and inspected their network requests before considering HTML parsing. No concert/event API or structured event feed was requested; the only source-hosted dynamic endpoint observed was unrelated Instagram data.
- Inspected the rendered schedule page and its raw HTML. It contains no embedded Bandsintown widget, event JSON, JSON-LD Event objects, pagination, date-range controls, genre/category filters, or individual concert detail links.
- Inspected the site's sitemap for another event or archive page. It exposes the same `/schedule` page but no concert archive or event-detail collection.
- Followed the official `https://www.bandsintown.com/a/15631136-deniz-turkmen` schedule link in Playwright and requested it directly with a browser user agent. Both approaches were blocked by Cloudflare with HTTP 403, so its HTML and network API calls could not be inspected.
- Considered the four dated masterclasses shown on `/schedule`; they are fully booked educational sessions without evidence of a substantial public live performance, venue, or city, so they cannot yield valid in-scope records.

## Filters and feed assessment

The official site exposes no first-party genre, category, discipline, event-type, series, or tag filters and no paginated event feed. There were therefore no filter values or pagination persistence to test. The source itself represents a classical pianist and the advertised tour is classical-only, but there is no scrapeable first-party concert feed from which valid records can be constructed. No upload target can be selected until event records are accessible.

## What would unblock implementation

Implementation would become possible if the official site embedded concrete tour dates (including venue and city), exposed a structured event feed, or the linked Bandsintown artist page/API became accessible without an unavailable credential or Cloudflare challenge. At that point, the event coverage and any first-party filters should be re-evaluated before choosing the upload target.
