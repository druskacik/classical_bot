<!-- crawler-factory-metadata
{"url":"https://miroslavbeinhauer.com/","geographic_scope":"country","country_code":"CZ","reason_code":"no_current_events","attempted_at":"2026-08-22","retry_after":"2026-09-21"}
-->

# No scrapeable concert listings

Original URL: https://miroslavbeinhauer.com/

The website is the official site of Czech pianist and sixth-tone harmonium
player Miroslav Beinhauer, but it does not currently publish a concert calendar
or an archive of concrete concert occurrences. Its public content is limited to
biography, repertoire, discography, and contact pages. Those pages mention past
performances in biographical prose but do not provide event-level combinations
of a real date, venue, and city, so they cannot produce valid crawler records.

## Investigation performed

- Inspected the initial page and its network requests with Playwright. The page
  made only ordinary WordPress document and static-asset requests; no calendar,
  event API, JSON feed, or asynchronous event request was present.
- Queried the first-party WordPress REST API through its working query-string
  route (`?rest_route=/wp/v2/...`). The exposed content types are standard
  WordPress types only. The posts endpoint reports zero posts, while the pages
  endpoint reports 11 static Czech/English pages: biography, repertoire,
  discography, contact, and empty home-page variants.
- Checked the WordPress categories and tags endpoints. They contain no populated
  event taxonomy: both categories have zero posts and the tags collection is
  empty. No first-party genre, category, discipline, event-type, series, or tag
  filters applicable to concerts are exposed.
- Checked the rendered navigation and HTML for both language variants. Neither
  contains an events, concerts, programme, schedule, or archive section.
- Tried the standard WordPress and Yoast sitemap locations
  (`/wp-sitemap.xml` and `/sitemap_index.xml`); both return 404.
- Tried the site's own WordPress searches for `concert` and `koncert`. They do
  not reveal an event collection or dated event pages; matches can only come
  from the static informational content.

There is therefore no selected feed, pagination, or date-range behavior to
validate, and no upload target can safely be chosen. The site is artist-specific
and its subject matter is classical-only, but biographical and repertoire pages
are not concrete performances under the project's inclusion guidance.

## What would unblock implementation

Implementation can proceed if the site adds a public concert calendar or
archive whose entries expose concrete dates and defensible venues and cities,
whether as WordPress posts/pages, a stable API/feed, or parseable HTML. A
first-party external calendar linked from this site with those fields would also
be sufficient.
