<!-- crawler-factory-metadata
{"url":"https://www.osceolacommunityorchestra.org/","geographic_scope":"country","country_code":"US","reason_code":"no_parseable_source","attempted_at":"2026-08-19","retry_after":"2026-09-18"}
-->

# Osceola Community Orchestra crawler blocked

## Original URL

https://www.osceolacommunityorchestra.org/

## Why a crawler cannot currently be implemented

The organization's concert schedule is published as text embedded in a raster
poster (`image/OCO_Upcoming2026.jpg`), not as HTML or structured data. The poster
contains four concrete 2025–26 concerts, but neither the page nor the image has
an alt-text or metadata representation of their titles and dates. The project's
production dependencies and runtime do not provide OCR, so a universal crawler
cannot reliably extract the schedule when the poster changes.

The surrounding HTML contains two dated historical video captions (March 28,
2023 and March 26, 2024), but scraping only those would knowingly omit the four
concerts advertised by the schedule poster and would therefore not cover the
source.

The source is a US community orchestra based in St. Cloud, Florida, so the
resolved geography is country scope with country code `US`. It is a
classical-only orchestral source. The site exposes no genre, category,
discipline, event-type, series, or tag filters.

## Investigation performed

- Loaded the home page with Playwright and inspected its network requests.
- Found no XHR/fetch request, event API, JSON feed, calendar feed, or pagination;
  the page is static HTML and media assets.
- Inspected all first-party navigation links (`home.html`, `gallery.html`, and
  `contact.html`) and the rendered home-page content.
- Inspected the schedule image directly. It advertises four free orchestra
  concerts at St. Cloud Community Center: October 28 and December 9 in 2025,
  and March 10 and June 2 in 2026, all at 7 PM.
- Inspected the HTML source. The current schedule details exist only in the
  image; HTML supplies only the venue context and two older video captions.
- Checked the repository's declared dependencies and runtime for an OCR tool;
  none is available.

There are no first-party filters or pagination parameters to test. Consequently
there is no feed selection or upload target: a crawler cannot emit complete,
reliably parsed records from the available source.

## What would unblock implementation

Any stable first-party machine-readable representation of the schedule would
unblock the crawler, including event HTML, JSON/API data, an iCalendar feed, or
meaningful image alt text. Alternatively, adding a supported OCR dependency and
runtime to the shared production environment could make the poster parseable,
though the parser would still need validation against future poster layouts.
