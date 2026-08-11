<!-- crawler-factory-metadata
{"url":"https://ensemblevocalorphee.com/","geographic_scope":"country","country_code":"CH","reason_code":"no_parseable_source","attempted_at":"2026-08-11","retry_after":"2026-09-10"}
-->

# Crawler blocked

Original URL: https://ensemblevocalorphee.com/

The source is the Geneva-based Ensemble vocal Orphée, so its resolved geography is Switzerland (`CH`). Its concerts are in project scope, but the site does not currently publish enough machine-readable event data to build a reliable crawler. Concert dates, times, venues, cities, and most programme details are printed only inside poster images. The surrounding page markup exposes the images but not those required facts. A crawler based on image filenames would produce incomplete or invalid records.

## Approaches attempted

- Inspected the initial page and concert calendar network traffic with Playwright. No event-specific XHR, GraphQL, AJAX, or calendar API was called.
- Reconstructed and queried the public WordPress REST API at `/wp-json/wp/v2/pages`, including the French `concerts` page and English `concerts-3` page. The API is stable, but its page content consists of Divi shortcodes containing poster-image URLs and occasional ticket links; it has no structured event objects.
- Queried `/wp-json/wp/v2/media` for representative current posters. Attachment metadata contains only filenames, dimensions, and empty captions/alt text, not event dates, times, venues, cities, or descriptions.
- Inspected the rendered French and English concert pages and their archives. The DOM contains poster images and year headings. With isolated exceptions in prose for older concerts, it does not contain the required event details as text.
- Tested a first-party-linked Geneva ticketing product redirect (`id=10229251611223`), which returned a 404 error page. Other poster entries have no detail-page or ticket link at all.
- Checked the archive through 2017. It remains primarily image-based and does not offer consistently parseable occurrence data.

The source is a classical vocal ensemble and the concert page is classical-only. It exposes no genre, category, discipline, event-type, series, or tag filters, and no pagination. Consequently, filter persistence and adjacent-filter contamination cannot be tested; filtering is not the blocker.

## What would unblock implementation

Implementation would become possible if the publisher adds semantic event text or structured data (for example JSON-LD with start date and location), exposes a stable event/ticket API, provides working detail pages with the required fields, or supplies reliable textual captions alongside every poster. A production-supported OCR service with stable output and explicit project integration could also make the image-only archive usable.
