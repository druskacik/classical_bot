<!-- crawler-factory-metadata
{"url":"https://www.pulcinella-orchestra.fr/","geographic_scope":"country","country_code":"FR","reason_code":"no_current_events","attempted_at":"2026-08-18","retry_after":"2026-09-17"}
-->

# Crawler blocked: no scrapeable concert calendar

## Original URL

https://www.pulcinella-orchestra.fr/

## Why a crawler cannot currently be implemented

The source is the French-based Pulcinella Orchestra and is therefore resolved to
country code `FR`. Its public Agenda page currently contains no event listings,
including no past-event archive. Consequently, the site does not expose any
concert occurrence with the complete required combination of date, city, and
venue.

The homepage contains one old promotional item for “Battle baroque à Pontoise”
with “13 et 14 nov” and an outbound Festival baroque de Pontoise link. The
first-party page does not state a year or a concrete venue, so those occurrences
cannot be emitted without inferring required fields from an external URL slug.
They were therefore treated as incomplete rather than scrapeable records.

Pulcinella Orchestra is a classical-only source, but no upload target is usable
until the site publishes valid concert occurrences.

## Investigation performed

- Loaded the homepage and `/agenda/` with Playwright and inspected their network
  requests. The agenda made no calendar, event, JSON, GraphQL, or AJAX request;
  no reconstructable event API was present.
- Inspected the first-party WordPress REST API. `/wp-json/wp/v2/types` exposes
  pages, posts, media, and portfolio items, but no event post type.
- Queried agenda page ID 664 through `/wp-json/wp/v2/pages?slug=agenda`. Its
  rendered content contains only the agenda hero/header and divider, with no
  hidden event markup or embedded calendar data. Public revision access returns
  HTTP 401.
- Enumerated all published pages, posts, and portfolio items. The portfolio
  entries are repertoire/project, personnel, recording, or media pages rather
  than dated concert occurrences; the posts are placeholder/demo content.
- Inspected the exposed categories, tags, and `portfolio_entries` taxonomies.
  There are no first-party genre, discipline, event-type, series, or concert
  filters applicable to an event feed, so no filter values or pagination
  persistence could be tested. The API collections each fit on one page and do
  not provide an event date-range feed.
- Inspected the rendered agenda HTML as a fallback. It likewise contains no
  event cards, structured event data, pagination, archive links, or concert
  details to parse.

## What would unblock implementation

Implementation can proceed when the Agenda page publishes concrete events with
valid dates and identifiable venues/cities, or when the site exposes a public
calendar/API/archive containing those fields. A public historical agenda or
accessible WordPress revision containing complete occurrences would also make a
past-event crawler possible.
