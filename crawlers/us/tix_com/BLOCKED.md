<!-- crawler-factory-metadata
{"url":"https://www.tix.com/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# Tix.com crawler blocked

## Original URL

https://www.tix.com/

## Why a crawler cannot currently be implemented

Tix.com is a US-based, mixed ticketing marketplace, but its public pages return
an HTTP 403 Cloudflare challenge to both an automated browser and ordinary HTTP
clients. The challenge did not resolve in Playwright, so neither a complete
candidate catalog nor individual event details can be fetched reliably.

The public search page is keyword-based rather than a comprehensive first-party
genre or event-type feed. Search-engine-visible examples show unrelated theatre,
sports, tribute, pass, and other inventory. Consequently, a keyword query would
not locate all eligible classical, opera, ballet, crossover, family, and related
events, and it would not be safe to upload its results directly as classical.

## Approaches attempted

- Loaded the homepage and `search.aspx` with Playwright and inspected network
  requests. Both returned HTTP 403; only Cloudflare challenge and Turnstile
  traffic appeared, with no event API request to reconstruct.
- Waited for the browser challenge to resolve; it remained on the 403 challenge
  page.
- Loaded a known tenant catalog path (`/ticket-sales/vvtgnv/6616`) with
  Playwright. It was blocked by the same challenge.
- Requested the homepage, search page, a legacy schedule path
  (`/WicomicoSchedule.asp`), a tenant catalog path, and `secure.tix.com` with an
  HTTP client. Every request returned the Cloudflare 403 HTML instead of event
  content.
- Investigated indexed public pages and API references. Indexed Tix.com pages
  confirm that the source is mixed and expose only keyword search; similarly
  named public APIs found in search belong to other Tix/Tixly/TixTrack products
  or require tenant-specific credentials and cannot provide the Tix.com catalog.

## What would unblock implementation

Any of the following would permit another implementation attempt:

- allowlisted server access or a documented public Tix.com catalog API;
- a stable, unauthenticated feed that enumerates all current and archived
  marketplace events with pagination and event details; or
- a Cloudflare configuration change that permits automated read-only access to
  the public catalog and detail pages.

Because the source is mixed and exposes no verified comprehensive category
filter, an unfiltered accessible catalog should use `upload_target="potential"`
unless Tix later provides stable first-party filters whose combined coverage is
shown to match the project's inclusion scope without contamination.
