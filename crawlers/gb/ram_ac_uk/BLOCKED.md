<!-- crawler-factory-metadata
{"url":"https://www.ram.ac.uk/","geographic_scope":"country","country_code":"GB","reason_code":"access_blocked","attempted_at":"2026-08-12","retry_after":"2026-09-11"}
-->

# Royal Academy of Music crawler blocked

Original URL: https://www.ram.ac.uk/

The Royal Academy of Music is a UK institution whose events calendar is based in London, with occasional explicitly identified partner or touring venues. The resolved geography is therefore country scope, GB.

The site still publishes concrete current and archived concerts, but its Cloudflare managed challenge blocks automated access before the application is loaded. Both Playwright and ordinary HTTP requests receive HTTP 403 responses, so a production crawler cannot currently retrieve either listing or detail pages reliably.

## Approaches attempted

- Opened `https://www.ram.ac.uk/whats-on` with Playwright and inspected its network traffic first. The only non-static requests were the blocked document request and Cloudflare challenge/Turnstile requests; no first-party event API request was reached or reconstructable.
- Requested the home page, `/whats-on`, `/robots.txt`, and `/sitemap.xml` as HTML using a normal HTTP client. All tested first-party paths returned the same Cloudflare challenge instead of parseable site content.
- Checked the site's resolved Servd/Cloudflare hosting origin. The public origin alias did not provide a usable HTTPS endpoint, while direct-IP HTTP access remained behind Cloudflare.
- Examined publicly indexed listing and detail results to confirm that events exist and to understand the source. The listing is server-rendered and exposes pagination through `page=N` plus search parameters named `Search[category]`, `Search[date]`, and `Search[text]`. Indexed filter labels included `Show all`, `Free events`, `Resounding Shores`, `Lunchtime Concerts`, `Musical Theatre`, and `Partner Venues`. These are series or commercial groupings, not a complete stable genre taxonomy: they do not comprehensively separate eligible classical, opera, contemporary art music, crossover, and qualifying musical theatre from jazz, conferences, tours, and other non-performance events. Because live pages were inaccessible, exact category identifiers and persistence across pagination/date ranges could not be verified.
- Examined representative indexed detail content for a multi-performance classical programme (`Seen & Heard`) and an adjacent non-concert conference (`Women and Musical Histories; 1789–1914`). This confirms that the overall feed is mixed and would require `upload_target="potential"` unless a future investigation discovers a comprehensive stable first-party filter.

## What would unblock implementation

Implementation requires reliable crawler access to the public listing and detail HTML, or a documented/accessible first-party event API or calendar feed that is not intercepted by the Cloudflare challenge. Once access is available, the feed's category identifiers must be tested across pagination and date ranges. If no comprehensive in-scope filter exists, the appropriate selected feed is the complete concrete-event candidate feed with `upload_target="potential"`.
