<!-- crawler-factory-metadata
{"url":"https://www.jhf.cz/","country_code":"CZ","reason_code":"wrong_source","attempted_at":"2026-08-02","retry_after":null}
-->

# No scrapeable concerts

## Original URL

https://www.jhf.cz/

## Why a crawler cannot currently be implemented

The domain is the corporate website of JHF Heřmanovice spol. s r. o., a road-maintenance, stone-quarrying, and machinery business. It is not a classical-music or cultural-events website. The published site contains no concert listings, event calendar, programme, or archive from which valid concert records could be created.

## Approaches attempted

- Inspected the initial page load and subsequent network requests in Playwright. No XHR/fetch/API requests or structured event endpoints were present; the page only loaded ordinary static website resources.
- Inspected the rendered HTML and accessibility tree for concert, event, festival, programme, and archive surfaces. The only occurrence resembling an event term was “program” within prose about an EU operational funding programme, unrelated to concerts.
- Enumerated the site's navigation and internal links. The available pages are limited to the home page, products and services, three product detail views, and contact information. There is no events or archive route.

## What would unblock implementation

Provide the intended cultural organization's URL if `jhf.cz` was supplied in error, or retry if this domain is later repurposed and begins publishing scrapeable concert listings or an event archive.
