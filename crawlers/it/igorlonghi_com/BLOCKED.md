<!-- crawler-factory-metadata
{"url":"https://www.igorlonghi.com/","geographic_scope":"country","country_code":"IT","reason_code":"no_current_events","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# No scrapeable concerts

The original source is https://www.igorlonghi.com/. It is the official website
of Italian neoclassical pianist and composer Igor Longhi. The site is an artist
source rather than a multi-country event publisher, so its resolved geography
is Italy (`IT`), even though future appearances could be touring performances.

The homepage contains an **Upcoming concerts** section backed by the
Bandsintown widget, but the source currently exposes no concrete performances.
There are also no archived performances available from that widget, so a
crawler cannot currently produce records with the required real date, venue,
and city fields.

## Investigation performed

- Loaded the homepage with Playwright and inspected its network requests and
  rendered HTML. No concert request or concert cards were present.
- Inspected the WordPress REST API. The registered content types contain no
  event type, the posts collection is empty, and the only search result for
  `concert` is the homepage itself.
- Inspected the WordPress sitemap and page content. There is no event/archive
  page; the homepage embeds Bandsintown for artist `Igor Longhi` with past dates
  enabled.
- Reconstructed the widget API from the first-party widget JavaScript. The
  stable app identifier is `js_www.igorlonghi.com`. The upcoming request
  `/V3.1/artists/Igor%20Longhi/events/?app_id=js_www.igorlonghi.com` and the
  archive request with `date=past` both return `[]`. The artist endpoint reports
  `upcoming_event_count: 0`.
- No genre, category, discipline, event-type, series, or tag filters are
  exposed. Pagination could not be tested because both the current and past
  feeds contain zero records.

## What would unblock implementation

Implementation can proceed when the Bandsintown artist feed publishes at least
one concrete current or archived concert with a real date, venue, and city, or
when the website adds an HTML/API concert archive containing those fields. At
that point, representative records can be checked against the project scope and
the appropriate `classical` or `potential` upload target can be selected.
