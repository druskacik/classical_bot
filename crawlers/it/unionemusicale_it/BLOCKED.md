<!-- crawler-factory-metadata
{"url":"https://unionemusicale.it/","geographic_scope":"country","country_code":"IT","reason_code":"access_blocked","attempted_at":"2026-08-12","retry_after":"2026-09-11"}
-->

# Crawler blocked by CAPTCHA

## Original URL

https://unionemusicale.it/

The source is the Turin-based Fondazione Unione Musicale and its published
performances are based in Italy, so the resolved country is `IT`.

## Why implementation is currently blocked

Every tested first-party route responds with HTTP 202 and a SiteGround robot
challenge instead of concert data. The challenge redirects to
`/.well-known/sgcaptcha/` and requires an image CAPTCHA and cookies. This also
occurs in a full Playwright browser, so neither a request-based crawler nor an
HTML parser can currently obtain the source records reliably. A crawler based
on search-engine snippets would be incomplete and unstable and therefore is
not suitable for production.

The site does publish concrete current concerts and archives (search indexing
shows `/concerti/`, `/archivio-concerti/`, season pages, and individual
`/concerto/.../` pages), so this is not a `no_current_events` case.

## Approaches attempted

- Opened the canonical homepage with Playwright and inspected its network
  requests. Navigation ended at the SiteGround CAPTCHA; only challenge assets
  were requested, and no concert API or Ajax request was exposed.
- Tested the canonical and `www` hosts over HTTPS and HTTP. All variants
  returned the same challenge response.
- Probed likely WordPress discovery and API routes: `/wp-json/`,
  `/wp-json/wp/v2/types`, `/sitemap.xml`, and `/robots.txt`. Each returned the
  CAPTCHA interstitial rather than JSON, XML, or text.
- Tested the HTML calendar and archive routes `/concerti/` and
  `/archivio-concerti/`; access was blocked before their markup could be
  parsed.
- Verified via indexed first-party pages that the calendar exposes advanced
  filters for `Genere musicale`, `Stagione`, `Serie e proposte`, `Sale`, and
  concert date. Exact option values and their pagination behavior could not be
  inspected because the challenge blocks both page HTML and network requests.

## What would unblock implementation

One of the following is needed:

- allowlisting of the crawler's production egress address by the site owner;
- a stable first-party JSON, RSS, iCalendar, or other feed exempt from the
  challenge; or
- removal/configuration of the CAPTCHA so ordinary non-interactive requests can
  access the concert list, archive, and detail pages.

Once access is available, investigation should begin with the calendar's
network requests to identify the WordPress/Ajax filter identifiers, verify that
they persist across pagination and current/archive date ranges, and inspect all
genre/series options against the project inclusion guidance before selecting
the upload target.
