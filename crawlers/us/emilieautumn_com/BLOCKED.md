<!-- crawler-factory-metadata
{"url":"https://www.emilieautumn.com/","geographic_scope":"country","country_code":"US","reason_code":"no_current_events","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# No scrapeable concert source

The original URL is <https://www.emilieautumn.com/>. It is the official site of
US artist Emilie Autumn, but it does not currently publish a concert calendar or
an archive of concrete, scrapeable concert occurrences.

## Investigation

- Browser network requests on the home page exposed only site assets,
  analytics, and newsletter-related requests; no event or concert API was
  requested.
- The Squarespace JSON representation at `/blog?format=json` was inspected as
  the available structured first-party feed. It returned 20 posts on the first
  page and provided the stable next-page offset `1766190115028`. Requesting
  `/blog?format=json&offset=1766190115028` returned the remaining 17 posts and
  no further page, covering the full available 37-post archive.
- The only applicable first-party category tested was the exact category value
  `Events` (`/blog/category/Events`). It contains two posts: an invitation to an
  online group oracle reading and its replay. Neither is a music concert.
- The adjacent exact category value `Music` was also checked. Its live-related
  posts describe members-only online studio streams, event replays, recordings,
  or retrospective material. They do not provide physical concert occurrences
  with defensible venue and city values, and streaming/recording-only events are
  outside the project's inclusion scope.
- The XML sitemap was inspected for archived pages containing event, live,
  concert, tour, performance, or show terminology. It exposed no separate tour
  or concert collection and no qualifying detail pages.
- The rendered home page and navigation were inspected directly. They link to
  the blog, music, books, artwork, video, membership, story, newsletter, and
  shop, but provide no concert listing or archive.

## What would unblock implementation

Implementation can proceed if the official site adds a public tour/concert
calendar or a first-party feed whose records include concrete performance dates
and defensible venue and city information. A newly published archive meeting
those requirements would also unblock the crawler.
