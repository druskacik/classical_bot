<!-- crawler-factory-metadata
{"url":"https://londonsinfonietta.org.uk/","geographic_scope":"country","country_code":"GB","reason_code":"access_blocked","attempted_at":"2026-08-11","retry_after":"2026-09-10"}
-->

# London Sinfonietta crawler blocked

Original URL: https://londonsinfonietta.org.uk/

London Sinfonietta is a UK-based contemporary-classical ensemble. Its event
catalogue includes performances at its home venues and touring performances,
so the resolved source geography remains GB; a future crawler must use the
event's explicit country for an individual overseas tour record.

The site publishes concrete current and archived concerts under `/whats-on/`,
but Cloudflare currently returns an interactive challenge and HTTP 403 to both
browser and ordinary HTTP clients. A production crawler therefore cannot fetch
the catalogue or detail pages reliably.

Investigation attempted:

- Loaded the homepage and a representative current concert detail page with
  Playwright and inspected their network requests. Both returned HTTP 403, and
  the only dynamic traffic was to Cloudflare challenge/Turnstile endpoints; no
  event API request was exposed.
- Requested the first-party `robots.txt`, which is accessible and advertises
  `https://londonsinfonietta.org.uk/sitemap_index.xml`.
- Requested the advertised sitemap index, `/sitemap.xml`, and WordPress's
  `/wp-sitemap.xml` under both the apex and `www` hosts. All sitemap variants
  returned the same Cloudflare 403 challenge.
- Tested WordPress REST discovery and search through `/wp-json/`,
  `/wp-json/wp/v2/search`, and the `?rest_route=/wp/v2/search` variant. These
  were also challenged, so no structured API could be reconstructed.
- Tested the `/whats-on/` listing, its feed-style URL, and representative event
  HTML directly with browser and HTTP clients. Those pages were not parseable
  because the response was the challenge document rather than event HTML.
- Verified through public search indexing that the source does have concrete
  event pages, including current, past, educational, and overseas touring
  performances. This confirms that `no_current_events` is not the blocker, but
  search-result caches are not a stable first-party scrape source.

No applicable first-party category, genre, series, discipline, or event-type
filter could be tested because the catalogue application never loaded. Publicly
indexed pages indicate that `/whats-on/` is the organization's own performance
feed and appears classical/contemporary-art-music focused, but filter identifiers,
pagination, archive coverage, and possible contamination could not be verified.
Consequently no upload target can yet be selected safely.

Implementation can be unblocked by allowing the crawler's production HTTP
client through Cloudflare, disabling the challenge for public catalogue,
sitemap, and REST paths, or providing a stable first-party event API/feed that
is accessible without interactive challenge completion. Once access is
available, the `/whats-on/` feed and archives must be checked for pagination,
series filters, complete scope coverage, and touring venue geography before
choosing `classical` versus `potential`.
