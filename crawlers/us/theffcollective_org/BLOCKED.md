<!-- crawler-factory-metadata
{"url":"https://www.theffcollective.org/","geographic_scope":"country","country_code":"US","reason_code":"no_parseable_source","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# The FF Collective crawler is blocked

## Original URL

https://www.theffcollective.org/

The source belongs to The FF Collective, a classical and opera organization based in the San Diego, California area, so the resolved geography is the United States (`US`).

## Why a crawler cannot currently be implemented

The website no longer exposes a live, parseable concert source. The canonical HTTPS host fails during TLS negotiation with `ERR_SSL_VERSION_OR_CIPHER_MISMATCH`. Its HTTP endpoint is reachable but redirects to `http://www.theffcollective.org/`, where the home page and every tested content path return a Weebly/EditMySite `404 - Page Not Found` response.

A production crawler cannot discover current or past concerts from this state. Depending on the Internet Archive would substitute a third-party historical snapshot for the assigned first-party source and would not provide a reliable, updating feed.

## Approaches attempted

- Inspected browser network traffic for the canonical URL and both `www` and apex host variants over HTTPS and HTTP. HTTPS failed before application requests could be made; HTTP performed only the host redirect and loaded the generic Weebly 404 page. No concert API, JSON feed, XHR/fetch request, or event application endpoint was exposed.
- Tested likely discovery resources at `/sitemap.xml` and `/robots.txt`; both returned the same 404 page.
- Tested historical first-party paths including `/projects.html`, `/a-night-of-opera.html`, and `/fat-leonard.html`; all returned the same live 404 page.
- Queried the Internet Archive CDX index to verify the source identity and former structure. It lists historical first-party pages such as `/la-traviata.html`, `/don-giovanni.html`, `/sarah-tucker-in-concert.html`, `/fat-leonard.html`, and `/a-night-of-opera.html`, with captures through 2024. This confirms that the supplied domain previously hosted the relevant organization and concert pages, but those pages are no longer available from the live source.
- Checked public search results for the organization. They corroborate past classical/opera performances in Southern California but do not reveal a replacement first-party calendar or stable feed suitable for this crawler.

The unavailable site exposes no applicable first-party genre, category, discipline, event-type, series, or tag filters. Consequently, no filter values, pagination behavior, date-range persistence, feed coverage, or contamination could be tested, and no upload target can be selected safely.

## What would unblock implementation

Implementation can resume when the organization restores the domain with live event or archive pages, fixes HTTPS and serves its historical content again, or publishes a replacement first-party calendar/API URL. At that point the event discovery network requests and any category filters should be reassessed before choosing `classical` versus `potential` upload.
