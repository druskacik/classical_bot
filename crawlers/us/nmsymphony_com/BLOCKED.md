<!-- crawler-factory-metadata
{"url":"https://nmsymphony.com/","geographic_scope":"country","country_code":"US","reason_code":"no_parseable_source","attempted_at":"2026-08-16","retry_after":"2026-09-15"}
-->

# North Mississippi Symphony Orchestra crawler blocked

## Original URL

https://nmsymphony.com/

## Why a crawler cannot currently be implemented

The canonical concert schedule at `https://nmsymphony.com/schedule` publishes
concrete 2026–27 concert titles, dates, and descriptions, but it supplies no
venue for any listed concert and no individual first-party concert URL. Venue is
a required project field. A venue cannot be safely defaulted: first-party
programs from recent seasons show that the orchestra uses several different
Tupelo venues, including Link Centre Concert Hall, Tupelo Civic Auditorium,
Lyric Theatre, Harrisburg Baptist Church, and Tupelo High School Performing Arts
Center.

The schedule is a classical-only orchestra season, and the entries themselves
are in scope (including the live-orchestra Nutcracker and Disney programme).
There are no genre, category, discipline, event-type, series, or tag filters on
the site. The schedule is a single unpaginated page, so there are no filter or
pagination identifiers to test. Its visible 2026–27 schedule contains six
listings representing seven performance dates, but none can be emitted without
inventing a venue.

The first-party season brochure confirms and slightly expands the season dates,
but also omits venue and time information. The linked Eventbrite season-ticket
page is not a usable substitute: it represents a season pass rather than the
individual performances and gives the orchestra's office address as its
location, not the concert venues.

## Approaches attempted

- Inspected the home page, schedule page, legacy `/concert-schedule` URL,
  season-brochure page, PDF season brochure, and XML sitemap in Playwright.
- Inspected browser network requests before and after loading the schedule. The
  site exposed Squarespace analytics/button-render requests but no event API or
  structured concert feed.
- Inspected the rendered schedule HTML. Concert information is stored in static
  heading and paragraph blocks; no hidden structured data adds venues, times, or
  individual event URLs.
- Inspected the linked Eventbrite season-ticket page and organizer listing. The
  season-ticket occurrence has placeholder pass dates/times and the orchestra
  office as its location, while the organizer listing mixes concerts with
  sponsorship products and other non-event records.
- Checked the site's available archive surface. The sitemap exposes no old
  concert archive or individual concert pages. Recent first-party program PDFs
  demonstrate varying venues, making a site-wide venue default indefensible.

## What would unblock implementation

Any stable first-party source that associates each performance date with its
actual venue would unblock the crawler. Suitable options include updated
schedule blocks containing venue names, individual ticket/detail pages for each
concert, or a structured calendar/API feed with per-occurrence locations. Times
may remain absent, but venues cannot.
