<!-- crawler-factory-metadata
{"url":"https://opera-odeon.marseille.fr/","geographic_scope":"country","country_code":"FR","reason_code":"access_blocked","attempted_at":"2026-08-11","retry_after":"2026-09-10"}
-->

# Crawler blocked

## Original URL

https://opera-odeon.marseille.fr/

## Why implementation is currently blocked

The entire first-party site is currently behind a security/maintenance response. Every tested request returns HTTP 403 with the page title `Site en maintenance | Marseille.fr` and a message stating that the request was considered malicious. Consequently, no live programme response, event detail response, pagination response, or structured endpoint is available to implement and validate a working crawler against.

The source is based in Marseille, France, so its resolved geography is country scope with ISO country code `FR`.

## Approaches attempted

- Loaded the homepage with Playwright and inspected its network requests. The only document request returned HTTP 403; no programme API or other data request was made.
- Loaded `/sitemap.xml` with Playwright and inspected network traffic. It returned the same HTTP 403 maintenance/security page.
- Probed the first-party programme routes `/programmation-filtre` and `/programmation`, including pagination and the observed category query `field_categorie_principale_target_id=36`; all returned the same HTTP 403 response.
- Probed likely Drupal structured/static endpoints, including `/jsonapi`, `/robots.txt`, `/core/misc/drupal.js`, a known event-detail URL, and a first-party season PDF URL. They were also blocked by the same response.
- Retried with normal browser and crawler user agents and resolved the site's alternate advertised IPv4 address directly. Access remained blocked.
- Search-engine results confirm that concrete 2026–2027 programme pages were indexed recently and that the mixed programme exposed category filtering, but cached search text is not a stable or complete first-party source from which a production crawler can be implemented or its pagination and coverage verified.

## What would unblock implementation

Restore ordinary access to the first-party site (or allow this crawler's network), or provide a stable first-party programme API/feed that is reachable without the current block. Once reachable, the programme listing, category identifiers, pagination persistence, representative adjacent categories, event detail markup, venue/date extraction, and archive coverage can be inspected and tested before selecting the appropriate feed and upload target.
