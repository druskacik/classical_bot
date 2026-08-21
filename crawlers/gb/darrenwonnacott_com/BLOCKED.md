<!-- crawler-factory-metadata
{"url":"https://www.darrenwonnacott.com/","geographic_scope":"country","country_code":"GB","reason_code":"no_current_events","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# No scrapeable classical concerts

The original URL is https://www.darrenwonnacott.com/. The site is a UK-oriented portfolio for film, television, and video-game composer Darren Wonnacott, not a concert calendar. Its current pages and complete available news archive do not publish qualifying live classical concert occurrences with the required date, venue, and city fields.

## Investigation performed

- Inspected the rendered Home, News, About, Credits, and Music navigation with Playwright.
- Inspected browser network traffic first. No event API or calendar request appeared during normal page loads.
- Found and tested the site's public WordPress REST API. The API exposes only standard `post`, `page`, and `attachment` content types; it exposes no event or calendar post type.
- Retrieved the complete posts collection from `/wp-json/wp/v2/posts` using `per_page=100`. The response reported 115 posts and two pages, and the `page=2` pagination link returned the remaining 15 posts.
- Reviewed both API pages, covering the full archive. Entries concern composing credits, soundtrack or software-library releases, film releases, and film-festival/cinema screenings. Recording-only cinema screenings are outside the project's event-inclusion scope, and the archive contains no concrete qualifying live classical performances with defensible venue and city data.
- Checked first-party taxonomies. All posts have empty category and tag assignments; `/wp-json/wp/v2/categories` contains only an unused `Project` category (count 0), and `/wp-json/wp/v2/tags` is empty. Therefore the source exposes no applicable first-party genre, discipline, series, event-type, or tag filters to test across pagination.
- Inspected the HTML-rendered News archive and its `Older posts` pagination as a fallback. It presents the same WordPress post content and does not reveal a separate event feed.

## What would unblock implementation

Implementation would become possible if the site adds a concert/calendar section or API containing concrete live performances and enough first-party detail to extract a real date, venue, and city for each occurrence. A future retry should re-check the WordPress content types and the site navigation for such a feed.
