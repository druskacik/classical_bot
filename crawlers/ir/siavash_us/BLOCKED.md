<!-- crawler-factory-metadata
{"url":"https://siavash.us/","geographic_scope":"country","country_code":"IR","reason_code":"no_current_events","attempted_at":"2026-08-22","retry_after":"2026-09-21"}
-->

# No scrapeable concert events

The original source is https://siavash.us/. It is the official website of Iranian musician Siavash Shahsavari. Although the site has pages labelled “Events” and “Concerts,” it currently exposes no concrete concert occurrence with the required date, city, and venue fields. A working crawler therefore cannot presently return valid records.

## Investigation performed

- Inspected the homepage and its browser network requests. The only homepage API request was for an AudioIgniter music playlist; it is not an event feed.
- Inspected `https://siavash.us/Events/`. It contains music-release news rather than performances.
- Inspected `https://siavash.us/Concerts/`. It contains captions for historical concert photographs in Qazvin, Ankara, and other locations, but no occurrence dates or usable venue details.
- Followed the first-party “Tour Dates” link to the artist's Bandsintown page. Its upcoming feed reports “No Upcoming Tour Dates.” The Past tab calls `https://bnds.us/pastEvents` with stable artist ID `11393367` and returns one item dated 2019-07-07 in Karaj. That item describes the start of musical practice in preparation for a future concert, not a concrete public performance, so it is `not_event` under the project inclusion guidance.
- Inspected the WordPress REST API (`/wp-json/`), including registered content types, all three posts, searches across pages for “concert” and “event,” and the sitemap index. WordPress exposes no event/concert custom post type or event API route. An installed event-manager shortcode appears literally on `/all-events/` and yields no records.
- Inspected HTML-rendered pages and the WordPress post/page content. No category, genre, discipline, event-type, series, or tag filters applicable to performances are exposed. Consequently there are no filter values or pagination behavior to validate.

## What would unblock implementation

Implementation can proceed when the official site, its linked Bandsintown feed, or another first-party endpoint publishes at least one concrete performance with a real date, city, and defensible venue. Ideally the source would also expose stable event detail URLs and programme descriptions. The Bandsintown artist ID is stable and can be rechecked on the retry date.
