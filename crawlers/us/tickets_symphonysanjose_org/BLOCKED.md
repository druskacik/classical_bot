<!-- crawler-factory-metadata
{"url":"https://www.tickets.symphonysanjose.org/Online/default.asp","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# Symphony San Jose ticket calendar is access-blocked

## Original URL

https://www.tickets.symphonysanjose.org/Online/default.asp

The URL is Symphony San Jose's first-party Tessitura ticket calendar. It is a
US-based, classical-only source. Its concrete performances are primarily in San
Jose, California, with explicitly identified touring/local-area venues retained
on individual records (for example, Mission Santa Clara in Santa Clara).

## Why a production crawler cannot currently be implemented

Cloudflare serves the populated concert listing only after a browser completes
its JavaScript challenge and receives a `cf_clearance` cookie. System Python's
ordinary `requests` client and the repository's `curl_cffi` client (tested with
Chrome, Chrome 120, and Safari impersonation) all receive HTTP 200, but the
response is only the empty ticket-site shell: it contains the search form and
navigation but no `.result-box-item` concert elements. A production crawler
using the repository's HTTP interfaces would therefore consistently return no
records.

Playwright can complete the challenge and confirms that concerts are currently
published, so this is not an empty-calendar condition. The browser-only result
cannot be converted into a durable HTTP request because the clearance token is
short-lived and bound to the challenged client.

## Investigation performed

- Inspected the Playwright network log before considering HTML parsing. No JSON,
  GraphQL, XHR, or other structured event API was requested. Event data is
  embedded in the initial server-rendered HTML after challenge clearance.
- Inspected the populated HTML. Concrete performances are represented by
  `.result-box-item` nodes with `.item-name`, `.start-date`, `.item-venue`, and
  `.item-teaser` fields. The same feed also contains undated subscriptions and
  passes, which are identifiable and could be skipped because they lack a real
  occurrence date.
- Confirmed three result pages. Pagination uses
  `BOset::WScontent::SearchResultsInfo::current_page` values `2` and `3`, plus
  the stable article ID `64C39D67-4069-46E6-B3B1-060BB7601A7A`; it also requires
  a session-specific `sToken`, so those links cannot be replayed independently.
- Tested the first-party date-search fields
  `BOset::WScontent::SearchCriteria::search_from=01/01/2020` and
  `BOset::WScontent::SearchCriteria::search_to=08/20/2026`. The site normalized
  them to `1/1/20` and `8/20/26` and returned no archived performances. Thus no
  past events were exposed for that range.
- Inspected the hidden first-party filter fields. The listing exposes
  `venue_filter`, `city_filter`, `month_filter`, `object_type_filter`, and
  `category_filter`, but provides no selectable values for any of them on this
  installation. No applicable genre/category filter is exposed. The unfiltered
  feed is nevertheless appropriate in scope because it is Symphony San Jose's
  own ticket inventory; representative records included orchestral concerts,
  chorale concerts, and orchestral Nutcracker performances.
- Opened a representative performance (`The New World SATURDAY`) through
  Playwright. Its seat-selection page confirmed the title, California Theatre,
  San Jose, and the October 3, 2026 7:30 PM occurrence, but navigation depends on
  session state and does not expose a stable public performance URL.
- Investigated the linked first-party `symphonysanjose.org` season site as a
  fallback. Its WordPress REST search exposes page titles and URLs (including
  current and past seasons), but the page bodies and event date fields are stored
  in non-public Avada metadata; `content.rendered` is empty. Direct HTML pages
  are also Cloudflare-gated, so this route cannot supply complete occurrences.

## What would unblock implementation

Any of the following would make a reliable crawler possible:

- removal or relaxation of the Cloudflare JavaScript challenge for read-only
  calendar pages;
- a documented/public Tessitura event API or stable server-rendered feed that
  does not require browser clearance and session-bound tokens;
- public WordPress REST fields containing each concert's dates, times, venue,
  city, and programme text; or
- an approved browser runtime available to production crawlers.

If access is unblocked, the populated first-party Symphony San Jose feed should
use `upload_target="classical"`: the source is classical-only, while undated
passes and subscription products can be excluded structurally rather than sent
as events.
