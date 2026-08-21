<!-- crawler-factory-metadata
{"url":"https://www.gillianwhitehead.co.nz/","geographic_scope":"country","country_code":"NZ","reason_code":"no_current_events","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# Crawler blocked: no scrapeable concert calendar

The original URL is https://www.gillianwhitehead.co.nz/. It is the official New Zealand website of composer Dame Gillian Karawe Whitehead, so the resolved geography is New Zealand (`NZ`).

The site does not currently expose a concert or performance calendar, including an archive from which valid concert occurrences can be reconstructed. Its News page contains editorial summaries of recordings, awards, and some already-completed performances, but those summaries do not provide the complete occurrence-level date, city, and venue data required by this project. The composition catalogue must not be interpreted as a concert feed.

## Investigation performed

- Loaded the home page and News page with Playwright and inspected their network requests. No event API, calendar request, or asynchronous event feed was made; the only non-static requests were analytics/recaptcha traffic and a broken Google Maps script URL.
- Inspected the public WordPress REST API at `/wp-json/wp/v2/types`. It exposes ordinary posts and pages plus `recording` and `publication` custom post types, but no event, concert, performance, calendar, or occurrence post type.
- Inspected `/wp-json/wp/v2/posts` and its first 100 records. These are individual musical works categorized by forces such as Orchestra, Opera, Choral music, Dance, and Chamber ensemble. Their publication dates are catalogue metadata, not performance dates, and their API content is empty.
- Inspected `/wp-json/wp/v2/pages` and the rendered HTML for `/news/`. The News page is a manually curated page rather than a paginated event archive. Its current performance-related item retrospectively mentions three May performances in Tauranga, Rotorua, and Hamilton without occurrence dates or venues.
- Checked the site's navigation and representative links. They lead to biography, music, recordings, publications, contact, individual composition pages, recording pages, and third-party articles or presenter pages—not a first-party occurrence feed.

## Filters and coverage

No applicable first-party genre, category, discipline, event-type, series, or tag filters exist for concert occurrences, and there is no event pagination or date-range behavior to test. The WordPress categories are instrumentation/work-catalogue filters, not event filters. Scraping them would create non-event records and would still lack required venue and city fields.

## What would unblock implementation

Implementation can proceed if the site adds a first-party events calendar or archive whose listing/detail pages (or API) expose concrete performances with real dates and defensible venues and cities. A structured feed maintained by the site, including links to first-party occurrence details, would also be sufficient.
