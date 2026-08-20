<!-- crawler-factory-metadata
{"url":"https://www.thegilmore.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# The Gilmore crawler is blocked

## Original URL

https://www.thegilmore.org/

The source is the US-based Irving S. Gilmore International Piano Festival. Its
calendar includes performances in Michigan cities such as Kalamazoo, Ann Arbor,
Jackson, Battle Creek, and Grand Rapids, so the resolved crawler geography is
the United States (`US`).

## Why implementation is currently blocked

Cloudflare returns an HTTP 403 challenge page instead of source content to both
the Playwright browser and ordinary Python HTTP requests. The browser's
challenge flow does not produce an accessible event page, and the same block
applies to the site's WordPress and Events Calendar API routes. A production
crawler implemented against any of these routes would parse a challenge page or
fail every run, rather than return concerts reliably.

The source does publish scrapeable concerts in principle, including past-event
archives, but none of the tested first-party representations is currently
accessible from the crawler environment.

## Approaches attempted

- Loaded the homepage and event routes in Playwright and inspected their
  network requests. The only application request returned 403; subsequent
  traffic was Cloudflare challenge/Turnstile traffic, with no event API request.
- Tested the first-party concert listing and The Events Calendar category
  archives, including `/concerts-events/`, `/events/`, the `2026-festival`,
  `solo-piano-2026-festival`, and `jazz-2026-festival` category values, plus
  `tribe-bar-date=2026-04-01`. All returned the challenge, so persistence across
  pagination or date ranges could not be verified directly.
- Tested the calendar export at `/events/?ical=1`; it was also challenged.
- Tested likely structured endpoints:
  `/wp-json/tribe/events/v1/events`,
  `/wp-json/wp/v2/tribe_events?per_page=5`, and
  `/wp-json/wp/v2/types`. All returned HTTP 403 challenge HTML.
- Tested the bare domain and alternate event/API-style subdomains. The bare
  domain redirects to the challenged canonical `www` host; no separate public
  event API was found.
- Search-indexed first-party pages confirm a mixed programme with classical,
  jazz, films, talks, theatre, master classes, family events, and partner events.
  Consequently, an unfiltered feed could not safely upload directly to
  `classical`. The available evidence suggests `potential` would be required
  unless a future investigation can verify and combine comprehensive stable
  classical-scope category identifiers without contamination.

## What would unblock implementation

Any stable first-party representation that is accessible to the production
crawler would unblock the work: allowlisting the crawler environment, relaxing
the Cloudflare rule for read-only calendar/API/ICS paths, or providing an
official public Events Calendar REST/ICS endpoint on an unchallenged host. Once
accessible, the category identifiers must be enumerated and verified across
past/future date ranges and pagination before selecting a filtered classical
feed; otherwise the mixed candidate calendar should use `upload_target='potential'`.
