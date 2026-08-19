<!-- crawler-factory-metadata
{"url":"https://www.springfieldmosymphony.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-19","retry_after":"2026-09-18"}
-->

# Crawler blocked by source access controls

## Original URL

https://www.springfieldmosymphony.org/

The source is the Springfield Symphony Orchestra in Springfield, Missouri,
United States. Search-indexed first-party pages show that the site currently
publishes concrete 2026–2027 season concerts and retains a past-concert archive,
so this is not an empty calendar or a repurposed domain.

## Why a crawler cannot currently be implemented

Cloudflare returns HTTP 403 before exposing usable page content to the crawler
environment. The response applies to both browser and direct HTTP clients, so a
production crawler would consistently fail without obtaining event records.
Search-engine snippets are useful for confirming that concerts exist, but they
are not a complete or stable first-party feed and cannot support a universal
crawler.

## Approaches attempted

- Loaded the canonical HTTP and HTTPS URLs, with and without `www`, in
  Playwright. Every variant returned HTTP 403.
- Inspected Playwright network traffic. Navigation ended at the blocked document
  request and exposed no application API or event-data requests to reconstruct.
- Tested likely first-party discovery and listing paths: `/robots.txt`,
  `/sitemap.xml`, `/concerts/`, `/events/`, `/calendar/`, and `/season/`. All
  returned HTTP 403.
- Tested the WordPress REST API root and likely API routes, including
  `/wp-json/`, `/wp-json/wp/v2/types`, and `/wp-json/wp/v2/concert`. All returned
  HTTP 403 rather than JSON.
- Repeated representative homepage, archive, and REST requests with a browser
  user agent using a direct HTTP client. Cloudflare again returned HTTP 403.
- Reviewed search-indexed first-party results for the homepage, `/concerts/`,
  `/events/`, `/new-season/`, and representative `/concert/.../` detail pages.
  These confirm a classical-only orchestra source with current and past concert
  occurrences, but do not reveal a scrapeable API or stable pagination/filter
  mechanism.

No applicable first-party genre/category filter or its exact values could be
tested because the source blocks the listing pages and API before their controls
or payloads load. Consequently, filter persistence across pagination and date
ranges could not be verified and no feed/upload target was selected.

## What would unblock implementation

Any of the following would permit another implementation attempt:

- the source allows the crawler environment through its Cloudflare policy;
- the site publishes an accessible first-party calendar feed or documented API;
- the operator provides an approved access method that works non-interactively
  in production; or
- a later retry finds that the current access rule has been removed.
