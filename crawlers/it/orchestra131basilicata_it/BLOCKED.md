<!-- crawler-factory-metadata
{"url":"https://orchestra131basilicata.it/","geographic_scope":"country","country_code":"IT","reason_code":"access_blocked","attempted_at":"2026-08-12","retry_after":"2026-09-11"}
-->

# Access blocked

Original URL: https://orchestra131basilicata.it/

The source is an Italian orchestra based in Basilicata, so the resolved crawler geography is Italy (`IT`). The site publishes concrete concert occurrences, including past events, through a WordPress installation using The Events Calendar. However, Cloudflare currently returns HTTP 403 challenge pages to both browser and normal HTTP clients, so there is no production-safe way to retrieve or validate the catalogue.

## Investigation performed

- Opened the original URL with Playwright and inspected its network traffic. The initial document returned HTTP 403 (`Just a moment...`), and the only subsequent dynamic requests were Cloudflare challenge/Turnstile endpoints. No event-data request or first-party API response was exposed.
- Tested the expected first-party The Events Calendar REST endpoint at `/wp-json/tribe/events/v1/events?per_page=5`, with and without the `www` hostname. Both returned Cloudflare HTTP 403 HTML rather than JSON.
- Tested the WordPress REST collection `/wp-json/wp/v2/tribe_events?per_page=5`. It also returned the Cloudflare HTTP 403 challenge.
- Tested the HTML past-events list `/eventi/elenco/?eventDisplay=past`. It likewise returned HTTP 403.
- Confirmed from indexed first-party pages that the site has a `Concerti` category and paginated/monthly archives with concrete events and detail pages. Indexed examples span past events in 2023–2025 and a January 2026 concert, but search-engine copies are not a complete or stable first-party feed and therefore are unsuitable as a crawler source.

The only applicable first-party filter found was the The Events Calendar category `concerti` (category slug/value `concerti`). Its indexed monthly URL form is `/eventi/categoria/concerti/YYYY-MM/`, and indexed list archives use `/eventi/elenco/pagina/N/` plus Tribe date/display parameters. Because Cloudflare blocks the live pages and API, the filter could not be tested live across pagination or arbitrary date ranges. Indexed representative results appear classical and performance-specific, but they cannot establish complete current coverage.

## What would unblock implementation

Allow non-interactive crawler traffic to the event HTML or either WordPress/Tribe REST API endpoint (for example by removing the challenge for those paths or allowlisting the crawler). Once accessible, the crawler can enumerate the full past and future Tribe Events collection, verify that the `concerti` category persists across API pagination/date ranges, inspect adjacent categories for eligible coverage, and parse each event's structured date, time, venue, city, description, and canonical URL.
