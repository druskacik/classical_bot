<!-- crawler-factory-metadata
{"url":"https://www.wrconcert.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# Crawler blocked

## Original URL

https://www.wrconcert.org/

## Why implementation is currently blocked

The registered domain is currently unavailable at the DNS level. DNS-over-HTTPS queries for both `wrconcert.org` and `www.wrconcert.org` return NXDOMAIN, while the Public Interest Registry RDAP record reports the domain status `client hold`. Consequently, neither the canonical HTTPS URL nor HTTP and apex-domain variants can be reached, and there is no live source from which a reliable crawler can be implemented or validated.

Search-engine investigation did not expose an indexed first-party calendar, event-detail pages, or a replacement canonical domain. There is therefore not enough evidence to identify any scrapeable current or archived concert catalogue, determine available first-party filters, or safely infer an API contract.

## Approaches attempted

- Browser-style opening of the supplied canonical URL; the host could not be resolved.
- Direct HTTPS and HTTP requests to the `www` hostname and HTTPS requests to the apex domain with redirects enabled; all failed during DNS resolution.
- DNS-over-HTTPS lookup of apex and `www`; both returned NXDOMAIN.
- Registry/RDAP inspection; the domain is registered, uses Wix nameservers, and is marked `client hold`.
- Searches for the exact domain, domain-restricted concert pages, organization identity, cached/indexed pages, and archived material; no usable first-party concert source was found.
- API/network investigation could not proceed because no page or application assets can be loaded. The requested Playwright MCP was not available in this execution session, but a browser would encounter the same DNS failure before generating application network requests.
- HTML parsing was evaluated as a fallback, but no live or cached first-party HTML was retrievable.

No genre, category, discipline, event-type, series, or tag filters could be inspected because the source is unreachable. No feed or upload target was selected.

## What would unblock implementation

Restore working DNS service for `wrconcert.org` / `www.wrconcert.org` by removing the registry hold and publishing valid records, or provide the organization's confirmed replacement first-party website or event API. Once reachable, the site should be reinvestigated for API endpoints, pagination, archives, event details, and first-party scope filters before implementing the crawler.
