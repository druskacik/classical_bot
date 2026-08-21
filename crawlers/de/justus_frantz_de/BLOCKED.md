<!-- crawler-factory-metadata
{"url":"https://justus-frantz.de/","geographic_scope":"country","country_code":"DE","reason_code":"access_blocked","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# Crawler blocked

The original source is <https://justus-frantz.de/>. It is the website of the
German pianist and conductor Justus Frantz. Although he tours internationally,
this is a Germany-based artist source, so the resolved country is `DE` rather
than a multi-country scope.

A reliable crawler cannot currently be implemented because the canonical
`www.justus-frantz.de` site returns a Cloudflare HTTP 403 challenge to automated
clients. The bare domain accepts a connection too slowly to return any response
within the investigation timeouts and redirects to the protected `www` host in
the browser. Consequently, neither event HTML nor a structured response is
available to a production crawler.

Investigation attempted:

- Playwright navigation to the supplied HTTPS URL; it timed out before receiving
  a document.
- Playwright navigation through the HTTP/`www` variant; this resolved to the
  canonical HTTPS `www` host and returned Cloudflare's “Attention Required” page
  with HTTP 403. Its network log contained only the blocked document request and
  no event API request.
- Direct HTTP `HEAD` and `GET` requests with a browser user agent; the bare host
  timed out without returning headers or HTML.
- Likely WordPress REST endpoints, both
  `/wp-json/wp/v2/pages?slug=tourdaten` and
  `/?rest_route=/wp/v2/pages&slug=tourdaten`; Playwright received the same
  Cloudflare HTTP 403 response for both.
- The indexed `/tourdaten/` page was inspected through search results as an HTML
  fallback. It exposes individually dated classical performances, but the
  visible entries are stale December 2024 dates. The indexed Finca Festival page
  is a 2025 festival overview and says its programme will be announced, so it is
  not a substitute feed of concrete occurrences.

No first-party genre, category, discipline, event-type, series, or tag filters
could be tested because the first-party pages and API are inaccessible. The
source itself appears classical-only, but no feed was selected and no upload
target can safely be exercised while access remains blocked.

Implementation would be unblocked by removal or adjustment of the Cloudflare
rule for ordinary read-only crawlers, a stable allowlisted JSON/WordPress API,
or another first-party calendar/feed that exposes the current and archived
concrete performances without an interactive challenge.
