<!-- crawler-factory-metadata
{"url":"https://www.modoantiquo.com/site/index.php","geographic_scope":"country","country_code":"IT","reason_code":"no_current_events","attempted_at":"2026-08-13","retry_after":"2026-09-12"}
-->

# No scrapeable concert occurrences

## Original URL

https://www.modoantiquo.com/site/index.php

## Why a crawler cannot currently be implemented

The website is an Italian early-music ensemble's promotional site, but it does not publish a calendar or any concrete concert occurrences with extractable dates, cities, and venues. Its pages titled “Programmi di concerto” are undated repertoire/programme proposals, not advertised performances. Turning those pages into events would create invalid records and season/programme overview false positives.

## Investigation performed

- Inspected the initial page and its browser network traffic. No first-party JSON, XHR, GraphQL, calendar, or other event API requests were exposed; the only non-document traffic was static assets, analytics, and Facebook resources.
- Checked both Italian and English navigation and followed the site's concert-programme sections for the Baroque Orchestra, Medieval Ensemble, and Bettina Hoffmann. These contain repertoire descriptions but no occurrence dates, cities, or venues.
- Checked `robots.txt` and `sitemap.xml`; both return HTTP 404.
- Inspected the site's internal navigation for archives, calendars, event categories, filters, pagination, and dated detail pages. None are exposed. Consequently there are no applicable first-party genre/category filters or stable filter values to test across pagination.
- Considered HTML parsing after the API investigation. The available HTML has no collection of concrete performances satisfying the project's required fields, including in the material presented as concert programmes.

## What would unblock implementation

Implementation would become possible if the source adds a public concert calendar or archive whose individual occurrences provide at least a real date plus a defensible city and venue. A documented or discoverable first-party event feed containing those fields would also unblock it.
