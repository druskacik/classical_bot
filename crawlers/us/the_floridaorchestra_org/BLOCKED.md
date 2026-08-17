<!-- crawler-factory-metadata
{"url":"https://the.floridaorchestra.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Access blocked

The original URL is https://the.floridaorchestra.org/. The source is The Florida Orchestra, a United States orchestra whose published performances are primarily in the Tampa Bay area, so the resolved geography is country scope with country code `US`.

A crawler cannot currently be implemented because all tested first-party machine-readable and HTML event routes require interactive anti-bot challenges. The assigned `the.floridaorchestra.org` host presents an Imperva "Additional security check" backed by hCaptcha. The canonical `floridaorchestra.org` event calendar redirects automated clients to SiteGround's `/.well-known/sgcaptcha/` robot challenge and returns HTTP 202 challenge HTML rather than event data. These challenges cannot be solved by a production crawler.

## Approaches attempted

- Loaded the original URL in Playwright and inspected its network requests before attempting HTML parsing. Only the Imperva challenge loaded; no concert API request was exposed.
- Investigated the publicly indexed WordPress Events Calendar pages at `/events/`, `/events/list/?tribe-bar-date=...`, `/events/month/YYYY-MM/`, and individual day views. Search-indexed copies show concrete performances, past-event navigation, and ten-event list pagination, but direct HTML requests are intercepted by the SiteGround challenge.
- Tested the Events Calendar REST API at `/wp-json/tribe/events/v1/events?per_page=5`, including the expected structured-events route. It returned challenge HTML instead of JSON.
- Tested broad historical date parameters on the list feed to establish that the calendar design exposes archives. The stable `tribe-bar-date=YYYY-MM-DD` value is visible in indexed pagination URLs, but it cannot be verified end-to-end while access is blocked.
- Examined the first-party filter UI visible in indexed pages. It exposes `Event Category` and `Tags`, but exact option values/IDs and their persistence across pagination cannot be inspected behind the challenge. Indexed representative entries also show that the unfiltered calendar contains non-concert records such as a guild award and potentially ambiguous masterclasses alongside Masterworks, Pops, chamber orchestra, film concerts, family, special, and community concerts. Therefore, absent verified comprehensive filter values, any eventual unfiltered feed should use `upload_target="potential"`.

Implementation would be unblocked by allowlisting the crawler's production egress, providing a documented challenge-free first-party API/feed, or removing the automated-client challenge from the Events Calendar HTML or WordPress REST endpoints. Once accessible, the REST API should be investigated first; otherwise the date-paginated list HTML can be parsed, with each listed performance occurrence expanded into its own record.
