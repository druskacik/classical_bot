<!-- crawler-factory-metadata
{"url":"https://tickets.kcsymphony.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Kansas City Symphony ticket calendar is access-blocked

## Original URL

https://tickets.kcsymphony.org/

The source is the United States-based Kansas City Symphony ticket calendar. Its
events are principally in Kansas City, Missouri, with occasional touring
performances, so the resolved crawler geography is `US`, not multi-country.

## Why a crawler cannot currently be implemented

Every request to the ticketing site is intercepted by an Imperva human-verification
page. The response contains no event catalogue or event-detail data and requires
an hCaptcha interaction. A production crawler cannot reliably or appropriately
solve that interactive challenge.

The Symphony's accessible companion page at
`https://www.kcsymphony.org/upcoming-events/` is not an adequate substitute. It
contains curated highlights, genre/series summaries, subscription products, and
a short upcoming-performance teaser. Many cards combine several concrete
performances into a date range, omit occurrence times, or represent a season
package rather than a performance. Using it would systematically omit events and
would not yield a universal feed of concrete occurrences.

## Investigation performed

### Network and API approaches

- Loaded `/`, `/events`, and `/events/1000` with Playwright and inspected the
  captured network requests. Only the Imperva verification/challenge traffic was
  available; no event API request could be reached or reconstructed.
- Requested the same paths with a normal HTTP client. Each returned a small
  Imperva `NOINDEX, NOFOLLOW` challenge document rather than calendar HTML or
  JSON.
- Resolved the ticket hostname to its Tessitura deployment alias,
  `kcsm-tnew-prod.tnhs.cloud`, and requested `/events` through that hostname.
  Imperva returned the same challenge, so the origin alias does not expose a
  usable public endpoint.

### HTML approaches

- Inspected the browser-rendered ticket page. Its only content was the Imperva
  "Additional security check is required" page and hCaptcha iframe.
- Inspected the accessible first-party `www.kcsymphony.org/upcoming-events/`
  HTML. It confirms genuine concert content and ticket links but is a curated
  promotional page, not a complete occurrence calendar. It cannot reliably
  supply one record per performance with the required date, venue, and URL.
- Confirmed that the ticket calendar has previously exposed first-party keyword
  filters labelled `Helzberg Hall`, `Family Friendly Concerts` (also indexed as
  `Family Concerts`), `Special Concerts`, `Pops Concerts`, `Free Events`,
  `Classical Concerts`, `Holiday Concerts`, `Film + Live Orchestra Concerts`,
  `2025/2026 Season`, and `2026/2027 Season`. The challenge prevented discovering
  their exact request values or verifying persistence across pagination/date
  ranges. Consequently, no filter can safely be selected for production.

The organization presents a broad range of orchestra performances. Under the
project inclusion guidance, relevant coverage would need to combine classical,
pops/crossover, film-with-live-orchestra, family, holiday, special, and qualifying
free performances rather than use only the narrow `Classical Concerts` filter.
Because stable identifiers and complete pagination could not be verified, an
event feed—if access becomes possible—should initially use
`upload_target="potential"` unless representative checks prove a combined
first-party feed is comprehensive and uniformly in scope.

## What would unblock implementation

Any of the following would permit a retry:

- allowlisting non-browser crawler requests to the public calendar;
- a stable, unauthenticated Tessitura event/production/performance JSON endpoint;
- server-rendered calendar and event-detail HTML that does not require hCaptcha;
  or
- a complete first-party occurrence feed containing individual performance
  dates, times, venues, and detail URLs.

Once access is available, the filter request identifiers must be tested across
multiple date ranges and pagination pages, and representative results from every
relevant category must be checked for event-level coverage and contamination
before choosing the upload target.
