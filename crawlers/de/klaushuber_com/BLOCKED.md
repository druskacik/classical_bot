<!-- crawler-factory-metadata
{"url":"https://www.klaushuber.com/","geographic_scope":"country","country_code":"DE","reason_code":"wrong_source","attempted_at":"2026-08-21","retry_after":null}
-->

# Crawler blocked: source domain has been repurposed

## Original URL

https://www.klaushuber.com/

## Why a crawler cannot currently be implemented

The supplied domain now presents a German-language composition school and general music-education blog under the name “Klaushuber.” It is not a concert calendar or the site of a concert-presenting organization, and it exposes no concrete concert occurrences, including in its available archive. The live site identifies itself as “Hochschule für Musik Komponisten,” advertises composition courses, and lists a German telephone number. This is unrelated to the intended concert source, so the domain is treated as repurposed (`wrong_source`) rather than as a temporarily empty calendar.

## Investigation performed

- Inspected the initial page and its network requests with Playwright. No event API, calendar request, GraphQL endpoint, or other structured concert feed was requested; the only non-static requests were Google Translate resources.
- Inspected the site's advertised WordPress REST discovery link and requested `/wp-json/wp/v2/types`. The endpoint returned `404 page not found`.
- Requested the standard WordPress and SEO discovery endpoints `/wp-sitemap.xml`, `/sitemap_index.xml`, and `/feed/`. Each returned `404 page not found`.
- Tested the site's first-party query parameters `?s=konzert` and `?s=veranstaltung`. Both returned the unchanged home page rather than search results, so they are not usable filters.
- Inspected the HTML navigation, home page, contact page, and the Blog archive. The only archive is `/category/blog/`, with pagination to `/category/blog/page/2/`; its entries are educational articles rather than dated performances. No event, concert, genre, discipline, series, or tag filters are exposed.
- Checked the site's visible archive links and representative article titles. They cover composition study, arranging, music production, academic writing, and general classical-music articles, not concrete concerts with dates, cities, and venues.

## What would unblock implementation

Provide the current official URL for the intended Klaus Huber concert or work-performance calendar, or restore a first-party event/archive feed on this domain containing concrete performances with dates and defensible venue/location data.
