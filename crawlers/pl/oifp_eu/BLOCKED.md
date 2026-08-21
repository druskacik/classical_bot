<!-- crawler-factory-metadata
{"url":"https://www.oifp.eu/","geographic_scope":"country","country_code":"PL","reason_code":"access_blocked","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# Access blocked by Cloudflare

The original source is https://www.oifp.eu/, the website of Opera i Filharmonia
Podlaska in Białystok, Poland. The source currently publishes concrete concert
occurrences, but its Cloudflare challenge returns HTTP 403 to both a real
Playwright browser session and ordinary HTTP clients. A production crawler in
this repository would therefore be unable to retrieve its input reliably.

## Investigation performed

- Loaded the homepage and the calendar route
  `https://www.oifp.eu/kalendarz/?display=calendar` with Playwright and inspected
  their network requests. The only dynamic traffic exposed was Cloudflare's
  challenge/Turnstile traffic; no event-data API request was made before the
  challenge blocked the page.
- Tested the site's WordPress REST discovery endpoint, REST type endpoint, and a
  plausible `repertuar` post-type endpoint. All returned the same HTTP 403
  challenge instead of JSON.
- Tested the Yoast sitemap advertised by the accessible `robots.txt`, the
  WordPress feed, and `wp-admin/admin-ajax.php`. These were also challenged.
- Tested the canonical HTTPS host, the non-`www` host, and HTTP redirects. None
  provided an accessible HTML variant.
- Search-engine indexing shows current repertoire detail pages and concrete
  dated events, including opera, orchestral concerts, live-music film events,
  and unrelated items such as exhibitions and yoga. Thus the source is not
  empty, but the indexed snippets are not a stable or complete first-party feed
  and cannot support a universal crawler.

## Filters and upload-target assessment

The source is mixed: its calendar includes eligible classical performances and
out-of-scope non-performance events. No first-party genre, category, discipline,
event-type, series, or tag filter could be loaded or tested because Cloudflare
blocked both HTML and API discovery. Consequently, no exact filter values could
be verified across pagination or date ranges. If access becomes available and
no comprehensive stable first-party filter exists, the calendar candidate feed
must use `upload_target="potential"`; an unfiltered calendar must not upload
directly to the classical table.

## What would unblock implementation

Any stable first-party endpoint reachable by the production runtime would be
sufficient: allowlisting for the crawler, a documented public event API/feed,
or removal/relaxation of the Cloudflare challenge for calendar, sitemap, or REST
routes. Once accessible, the calendar's network traffic and WordPress content
types/taxonomies should be inspected first, with HTML detail-page parsing as the
fallback.
