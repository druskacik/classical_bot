<!-- crawler-factory-metadata
{"url":"https://sanathkumarnaibhi.com/","geographic_scope":"country","country_code":"IN","reason_code":"no_current_events","attempted_at":"2026-08-22","retry_after":"2026-09-21"}
-->

# No scrapeable concert catalogue

The original URL is https://sanathkumarnaibhi.com/. It is the personal website of Indian Carnatic violinist and creator Sanath Kumar Naibhi. The site publishes music videos, articles, podcasts, teaching offers, products, and production services, but it does not publish a calendar or archive of concrete concerts with occurrence dates, venues, and cities.

## Investigation performed

- Loaded the canonical HTTPS site with Playwright and inspected the rendered navigation, page content, links, and network requests. The initial document was protected by a JavaScript challenge, after which the site rendered normally. No event-feed or calendar API request appeared.
- Inspected the first-party WordPress REST API. `/wp-json/wp/v2/types` exposes normal posts, pages, media, and WordPress internal types, but no concert or event content type.
- Queried the WordPress search API for `concert`, `event`, `recital`, `performance`, and `live`, including archived content. `recital` returned no results. Other matches were articles, biography/teaching pages, music-video material, and services such as concert streaming; they were not concrete public performance occurrences with the required date, city, and venue.
- Inspected the first-party taxonomies. The only post category is `Blogs`; tags describe article subjects and do not provide a concert, classical, genre, discipline, series, or event-type feed. Therefore there are no applicable first-party event filters or pagination/date-range behavior to test.
- Inspected the rendered HTML navigation and homepage content as a fallback. It links to blog, services, products, biography, teaching, music videos, and contact pages, with no events or concert archive.

Because the source exposes no current or historical scrapeable concerts, neither a classical feed nor a potential-event candidate feed can produce valid records. Implementation would be unblocked if the site adds a public event calendar/archive or a stable first-party API/feed containing concrete performances with dates and defensible venue and city data.
