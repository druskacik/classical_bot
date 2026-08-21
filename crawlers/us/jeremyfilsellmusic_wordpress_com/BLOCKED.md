<!-- crawler-factory-metadata
{"url":"https://jeremyfilsellmusic.wordpress.com/","geographic_scope":"country","country_code":"US","reason_code":"no_current_events","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# No scrapeable concert calendar

The original URL is https://jeremyfilsellmusic.wordpress.com/. The source is Jeremy Filsell's professional website. Its current contact and institutional information places it in New York, United States, so the resolved country code is `US`.

A working concert crawler cannot currently be implemented because the site's Calendar link returns HTTP 404 and neither the live site nor its available archives expose concrete concert listings with the required event date, city, and venue fields. The only published post is a retrospective news item about a performance that had already occurred; its publication date is not the concert date and it is not a reusable event feed.

Investigation attempted:

- Inspected the live home page and Calendar link with Playwright. The Calendar URL (`/calendar/`) returned a WordPress “Page not found” response.
- Inspected browser network requests for a calendar or event API. No event request was made by the site.
- Queried the first-party WordPress.com REST API for published pages and posts. The pages collection contains biography, recordings, media, press, and contact pages but no calendar/event page. The posts collection contains one retrospective news post and no event archive.
- Checked the first-party WordPress sitemap. It lists the same non-event pages and single news post, with no current or archived concert URLs.
- Checked for usable first-party genres, categories, event types, series, or tags. No applicable event filters exist because there is no event feed; consequently pagination and date-range persistence cannot be tested.
- Considered HTML parsing, but the rendered HTML contains no calendar entries or stable event records to parse.

Implementation would be unblocked if the publisher restores a public calendar or exposes an API/feed containing concrete concert occurrences with dates and defensible venues and cities. A future retry should recheck `/calendar/`, the WordPress REST API, and the sitemap.
