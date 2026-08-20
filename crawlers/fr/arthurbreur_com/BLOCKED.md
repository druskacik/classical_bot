<!-- crawler-factory-metadata
{"url":"https://www.arthurbreur.com/","geographic_scope":"country","country_code":"FR","reason_code":"no_parseable_source","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# Arthur Breur crawler blocked

## Original URL

https://www.arthurbreur.com/

## Why a crawler cannot currently be implemented

The site's Performance Schedule says that Arthur Breur is currently scheduled
only for private performances and asks visitors to check back for future public
dates. It contains no event records.

The available archive has one concrete concert announcement, for the February
5, 2023 world premiere of *Amber the Ambitious Spider* in Tigard, Oregon. The
announcement supplies a date, time, city, orchestra, and programme context, but
does not identify the performance venue. A venue cannot be inferred safely from
the city or orchestra name. Since venue is a required crawler field, the only
archived concert cannot produce a valid record.

The resolved source geography is France: the site identifies Arthur Breur as a
France-based composer currently living in Serignan. The old US performance is
an archived engagement and does not make this a multi-country event source.

## Approaches attempted

- Inspected browser network traffic on the homepage and Performance Schedule.
  No concert, calendar, event, JSON, AJAX, or other structured event request was
  made; only analytics, media, and site-support traffic appeared.
- Inspected the rendered Performance Schedule HTML and structured data. The
  page contains the private-performances notice, a literal inactive
  `[calendar]` shortcode, no event links, and no Event JSON-LD.
- Queried the first-party WordPress REST API. The exposed content types contain
  no event type. The `concerts` post category has stable ID `71` and returns one
  post on one page (`X-WP-Total: 1`, `X-WP-TotalPages: 1`). That post is the
  venue-less 2023 premiere described above.
- Inspected the WordPress pages and posts exposed by the REST API, including the
  site sitemap and the adjacent composition and general blog content. These are
  portfolio, commission, biography, recording, or retrospective pages rather
  than additional concrete, parseable concert occurrences.

## What would unblock implementation

A future public event listing that supplies a real date, city, and venue would
make a crawler possible. Alternatively, adding the missing venue to the archived
2023 concert post would permit that past occurrence to be scraped. A restored
calendar/API with complete public-event records would also unblock the source.

