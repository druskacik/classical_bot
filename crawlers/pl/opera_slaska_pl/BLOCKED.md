<!-- crawler-factory-metadata
{"url":"https://opera-slaska.pl/","geographic_scope":"country","country_code":"PL","reason_code":"access_blocked","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# Opera Slaska crawler blocked

## Original URL

https://opera-slaska.pl/

Opera Slaska is based in Bytom, Poland, so the resolved crawler geography is
Poland (`PL`), not multi-country.

## Why implementation is currently blocked

The origin at `79.96.142.122` did not establish an HTTP response during this
attempt. Both the home page and a concrete monthly repertoire URL timed out in
Playwright before `DOMContentLoaded`. Independent HTTPS requests to the same
origin also timed out without receiving response headers. Consequently, no
live response body or network exchange was available from which to establish a
stable API contract or implement and validate an HTML parser.

Search-engine results show that the site does publish scrapeable concrete
performances, including a monthly repertoire, so this is an access failure
rather than an empty calendar or an unrelated source.

## Investigation performed

- Opened `https://opera-slaska.pl/` with Playwright and attempted to inspect
  its network request list. Navigation timed out without a document response,
  and the request inspector could not return a usable exchange.
- Opened the first-party monthly URL
  `https://opera-slaska.pl/repertuar/category/0/all/month/2026-09` with
  Playwright. It failed in the same way.
- Retried the origin with direct IPv4 HTTPS requests using a browser user agent;
  the TCP/HTTPS request timed out without headers or HTML.
- Reviewed indexed first-party routes. The repertoire uses route values of the
  form `category/0/all/month/YYYY-MM`; indexed September and June 2026 pages
  show concrete occurrences with venue information. The separate production
  catalogue exposes category IDs and slugs including `1/balet`, `2/koncert`,
  and `4/opera`, alongside Operetka, Musical, and Pozostale. These catalogue
  pages are not occurrence feeds and therefore cannot safely substitute for
  the inaccessible repertoire.
- The site is an opera-company source, but indexed adjacent material includes
  talks, gallery entries, and other non-performance records. Without live
  access, it was not possible to verify whether repertoire category values
  persist across month navigation, whether an API backs the calendar, or
  whether `category/0/all` contains only concrete performances. Selecting a
  direct-classical upload target would therefore be premature.

## What would unblock implementation

Restore HTTP access to the origin (or allow this crawler environment's egress
address), then repeat Playwright network inspection across multiple month URLs.
With live access, the crawler can verify any calendar API or fall back to HTML,
check the all-category feed and adjacent categories across past and future
months, inspect representative details and touring locations, and select
`classical` only if that feed is comprehensive and uncontaminated; otherwise it
should use `potential`.
