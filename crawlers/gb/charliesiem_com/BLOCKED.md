<!-- crawler-factory-metadata
{"url":"https://charliesiem.com/","geographic_scope":"country","country_code":"GB","reason_code":"access_blocked","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# Access blocked by Cloudflare

## Original URL

https://charliesiem.com/

Charlie Siem is a British classical violinist, so the source geography resolves
to the United Kingdom (`GB`). The site describes an individual touring artist;
touring internationally does not make the source itself multi-country.

## Why a crawler cannot currently be implemented

Every tested live route returns HTTP 403 and a Cloudflare "Performing security
verification" page. The challenge did not resolve in a real Playwright browser,
and the same block applies to ordinary HTTP clients. A production crawler would
therefore receive challenge HTML instead of concert data.

Historical captures show that the site has used WordPress and the Wolf Tour
Dates plugin, with a `/calendar` page, `show` detail pages, and a
`show-sitemap.xml`. Those archived captures are useful evidence of the former
site structure but are not an acceptable live, first-party production feed.

## Approaches attempted

- Loaded both `https://charliesiem.com/` and
  `https://www.charliesiem.com/` in Playwright and inspected their network
  requests. Only Cloudflare challenge traffic was exposed; no concert API or
  application-data request was made.
- Waited for the browser verification and inspected the resulting page. It
  remained an HTTP 403 challenge page.
- Requested the home page, `/calendar`, `/events`, `/concerts`, `/tour`,
  `robots.txt`, `sitemap.xml`, `sitemap_index.xml`, and
  `show-sitemap.xml` through an HTTP client. The live origin returned the same
  Cloudflare challenge rather than parseable HTML or XML.
- Tested WordPress REST discovery and likely structured endpoints, including
  `/wp-json/`, `/wp-json/wp/v2/pages`, `/wp-json/wp/v2/types`,
  `/wp-json/wp/v2/show`, and the `rest_route=/wp/v2/show` form. They are covered
  by the same block, so no stable API identifiers, filters, pagination, or date
  ranges could be verified.
- Inspected historical URL inventories to identify the past calendar and show
  implementation. This confirmed that concerts have been published, but did
  not provide a live scrapeable source.

No applicable first-party genre, category, discipline, event-type, series, or
tag filters could be tested because all live discovery and content endpoints
are blocked before application data is returned. Consequently, filter
persistence across pagination and date ranges, current coverage, and feed
contamination cannot be assessed, and no upload target can safely be selected.

## What would unblock implementation

Any stable first-party route that is accessible to unattended HTTP clients
would unblock the crawler: for example, allowlisting the crawler, relaxing the
Cloudflare challenge for the calendar/sitemap/REST routes, or publishing an
accessible JSON, RSS, iCalendar, or HTML concert feed. With access restored, the
calendar and WordPress `show` endpoints should be re-investigated first, and
their pagination, retained past events, location fields, and detail-page
descriptions verified before implementation.
