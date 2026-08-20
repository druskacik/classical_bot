<!-- crawler-factory-metadata
{"url":"https://apollotrio.com/","geographic_scope":"country","country_code":"US","reason_code":"no_current_events","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# Apollo Trio crawler blocked

## Original URL

https://apollotrio.com/

The organization is a United States-based chamber ensemble. Its biography describes an American concert career centered on New York and tours in the United States and Europe, so the resolved home-country scope is `US`, not multi-country.

## Why a crawler cannot currently be implemented

The live website publishes no scrapeable concert occurrences, either current or archived. It contains static biography, audio, photo, press, project, and contact material, but no schedule or calendar. Historical performances mentioned in prose do not provide the complete date, venue, and city required for valid records.

The HTTPS endpoint currently fails TLS negotiation, although the same site is accessible over HTTP. This does not itself prevent scraping; the absence of concert data does.

## Approaches attempted

- Loaded the site with Playwright and inspected its network requests. The page makes only WordPress theme, plugin, image, and script requests; it makes no event or calendar API request.
- Inspected the first-party WordPress REST API at `/wp-json/`. Its exposed content types are only posts, pages, and attachments; there is no event custom post type or event-specific API route.
- Enumerated the API collections. `/wp-json/wp/v2/pages?per_page=100` reports seven static pages in one page of results, while `/wp-json/wp/v2/posts?per_page=100` reports only the default 2010 “Hello world!” post.
- Checked the visible navigation, WordPress archive, site search, `/wp-sitemap.xml`, and `/sitemap.xml`. Navigation has no events page, the only post archive is July 2010, and the sitemap endpoints return 404 pages.
- Reviewed the static Projects, biography, home, press, and audio content for concrete historical occurrences. They contain repertoire and narrative references to past performances, but not extractable records with a full date, venue, and city.
- Looked for first-party genres, categories, disciplines, event types, series, tags, and stable filter values. No applicable event feed or filters exist, so pagination and date-range persistence cannot be tested.

## What would unblock implementation

Publication of a first-party schedule or archive containing concrete concert occurrences with full dates and defensible venues and cities would unblock a crawler. A documented or discoverable event API exposing the same fields would also be sufficient.
