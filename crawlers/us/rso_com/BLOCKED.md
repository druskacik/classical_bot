<!-- crawler-factory-metadata
{"url":"https://rso.com/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Crawler blocked by site-wide robot challenge

## Original URL

https://rso.com/

The source is the Roanoke Symphony Orchestra in Roanoke, Virginia, United States.
It publishes concrete orchestral concerts, including archived event detail pages,
but the site does not currently provide an unattended scrapeable response from
the crawler environment.

## Investigation

- Opening the home page with Playwright redirected to
  `/.well-known/sgcaptcha/` and then `/.well-known/captcha/`, where SiteGround
  required an interactive CAPTCHA.
- Playwright's network log exposed only the robot-challenge document and its
  static challenge assets. No event-data request was made before the block.
- The site uses WordPress and The Events Calendar. A direct request to the likely
  structured endpoint, `/wp-json/tribe/events/v1/events?per_page=5`, returned
  HTTP 202 HTML that redirected to the same CAPTCHA instead of JSON.
- A request to `/wp-json/wp/v2/types` was blocked identically, so the general
  WordPress REST API is not an available fallback.
- The HTML calendar at `/concerts/` and its past-events form
  `/concerts/?eventDisplay=past` were tested. They also returned HTTP 202
  challenge HTML rather than calendar markup.
- Search-indexed representative pages confirm concrete events and The Events
  Calendar metadata (dates, times, categories, venues, and descriptions), but a
  search-engine index is not a reliable or complete feed for a production
  crawler.

## Filters and feed selection

No first-party genre, category, series, tag, or event-type filter could be
tested through pagination or date ranges because the challenge blocks both the
calendar HTML and REST API. Indexed event pages show categories such as
`Masterworks` and `Pops`, but their identifiers, completeness, adjacency, and
pagination persistence could not be verified. The organization is an orchestra
and its indexed Pops examples still feature the Roanoke Symphony Orchestra, but
no feed or upload target was selected without live representative access.

## What would unblock implementation

Any of the following would allow a crawler to be implemented:

- allowlisting the production crawler's egress address in SiteGround;
- disabling the CAPTCHA for the read-only WordPress REST event endpoint;
- a stable first-party JSON, iCalendar, RSS, or HTML calendar endpoint not
  protected by the interactive challenge; or
- first-party API credentials and documentation for an event feed accessible to
  the crawler service.

Once access is available, the preferred approach is to validate and paginate
`/wp-json/tribe/events/v1/events`, including past date ranges, inspect category
and series identifiers plus adjacent categories, and parse representative event
details before deciding between the `classical` and `potential` upload targets.
