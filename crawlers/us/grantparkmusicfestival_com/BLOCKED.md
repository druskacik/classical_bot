<!-- crawler-factory-metadata
{"url":"https://www.grantparkmusicfestival.com/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-18","retry_after":"2026-09-17"}
-->

# Access blocked by Cloudflare

## Original URL

https://www.grantparkmusicfestival.com/

## Why a crawler cannot currently be implemented

The source is the Chicago-based Grant Park Music Festival and publishes concrete
US concerts, but requests from the crawler environment do not reach the event
application. The canonical site returns HTTP 403 with a Cloudflare "Just a
moment..." challenge. Waiting for the browser challenge does not grant access.
Consequently there is no first-party response body whose structure can be
implemented and tested reliably.

Search-engine results confirm that the site currently has a 2026 event calendar
and archives, but cached search text is neither a complete nor a stable source
for a production crawler. A third-party cache or proxy was therefore not used as
the crawler feed.

## Approaches attempted

- Opened the canonical home page with Playwright and waited for the Cloudflare
  browser challenge. The page remained an HTTP 403 challenge page.
- Inspected Playwright network requests. They contained only the blocked document,
  Cloudflare challenge/Turnstile traffic, and static challenge resources; no event
  JSON or application API request was available to reconstruct.
- Requested likely structured and discovery endpoints directly, including
  `/wp-json/`, `/wp-json/wp/v2/types`, `/sitemap.xml`, and the sitemap advertised
  by `/robots.txt`. Application endpoints were challenged with HTTP 403. Only
  `robots.txt` was accessible, and it contains no event data.
- Investigated indexed first-party calendar URLs. The site exposes stable query
  parameters such as `date=2026-07-20` and event-type values including
  `instance_type=concert`, `instance_type=open-rehearsal`, and
  `instance_type=masterclass`. Indexed weekly pages show that these values persist
  in dated calendar URLs. The unfiltered calendar also contains pre-concert talks,
  while `concert` omits otherwise potentially eligible open rehearsals and
  performance-bearing masterclasses. None of these HTML pages can be fetched from
  the crawler environment because Cloudflare intercepts them.

## What would unblock implementation

Any of the following would allow a crawler to be built and validated:

- allowlisting the production crawler egress address or relaxing the Cloudflare
  rule for public event/calendar pages;
- a documented public first-party event API or calendar/ICS feed that is exempt
  from the challenge; or
- a stable first-party mirror of the calendar and event detail data accessible
  to non-interactive HTTP clients.

Once access is available, the calendar should be checked across its full date
range and the `concert`, `open-rehearsal`, `masterclass`, and adjacent event-type
feeds should be compared against the project inclusion guidance before selecting
the final feed and upload target.
