<!-- crawler-factory-metadata
{"url":"https://bangonacan.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-15","retry_after":"2026-09-14"}
-->

# Bang on a Can crawler blocked

## Original URL

https://bangonacan.org/

Bang on a Can is a US-based contemporary-music organization. Its event archive
also contains touring performances outside the US, but this does not make the
source genuinely multi-country; the resolved crawler geography is therefore US.

## Why implementation is currently blocked

Cloudflare returns HTTP 403 (`Attention Required!`) for the event calendar and
all tested machine-readable endpoints. The same block occurs in a real browser
session and from a plain HTTP client, so a production crawler cannot currently
retrieve the source reliably.

Public search indexing confirms that the site still publishes concrete upcoming
events and year-based archives, including `/events/`, `/events/2026/`, and older
year pages. Search snippets are not a complete, stable, first-party scrape input
and cannot safely substitute for the source pages.

## Approaches attempted

- Loaded `https://bangonacan.org/` and the event calendar with Playwright and
  inspected network traffic. The document request returned 403; the only
  additional dynamic request was Cloudflare's challenge endpoint, not an event
  data API.
- Tested likely WordPress/API and discovery endpoints: `/wp-json/`,
  `/wp-sitemap.xml`, and `/sitemap_index.xml`. Each returned the same 403 page.
- Tested HTML retrieval for `/`, `/events/`, and both the bare and `www` host
  using an HTTP client with a normal browser-style user agent. Each returned
  HTTP 403.
- Retrieved `/robots.txt`, the only tested first-party endpoint that returned
  HTTP 200. It contains crawler/content-signal rules but no event API or sitemap
  location.
- Checked indexed current and archived event pages to verify that scrapeable
  concerts exist conceptually. These results expose no stable genre, category,
  discipline, event-type, series, or tag filter and do not provide dependable
  pagination or full archive coverage.

No applicable first-party event filters or exact filter values could be tested,
and therefore no pagination persistence could be verified. No feed or upload
target was selected because the source cannot be fetched.

## What would unblock implementation

Any stable first-party access path that works without solving or bypassing the
Cloudflare challenge would unblock the crawler, for example an allowlisted
crawler user agent/IP, a documented public event API or calendar feed, or normal
HTML access to the event and yearly archive pages. Once available, the archive
and touring records must be parsed with per-event country codes and the event
feed must be assessed for category coverage before selecting `classical` versus
`potential` upload.
