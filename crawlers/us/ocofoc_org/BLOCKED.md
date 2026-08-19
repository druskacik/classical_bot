<!-- crawler-factory-metadata
{"url":"https://www.ocofoc.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-19","retry_after":"2026-09-18"}
-->

# Access blocked by the source

The original URL is https://www.ocofoc.org/. It belongs to the Orchestra
Collective of Orange County, a classical orchestra based in Orange County,
California, so the resolved geography is the United States.

A production crawler cannot currently be implemented because every tested
first-party route returns `403 Forbidden` from NinjaFirewall. The response says
that the crawler's public IP was blocked for security reasons, and no site HTML
or structured event data is exposed to parse.

## Investigation performed

- Loaded the canonical homepage with Playwright and inspected its network
  traffic. The only application request was the document request, which returned
  403 before scripts or API calls could load.
- Tested both `www.ocofoc.org` and `ocofoc.org`; both returned the same firewall
  response.
- Tested `/robots.txt` and `/sitemap.xml` as HTML and URL discovery paths; both
  were blocked with the same 403 response.
- Tested the WordPress REST API root at `/wp-json/`, the standard pages endpoint
  at `/wp-json/wp/v2/pages?per_page=5`, and a likely Events Calendar endpoint at
  `/wp-json/wp/v2/tribe_events?per_page=5`; all were blocked before returning
  JSON.
- Search-engine results confirm that the site still publishes concrete upcoming
  and archived concerts under `/concerts/` and `/past-concerts/`, but search
  snippets are not a stable first-party feed and cannot support a universal
  production crawler.

No first-party genre, category, discipline, event-type, series, or tag filters
could be inspected because the firewall blocks both the HTML application and API
discovery. Available indexed pages indicate that this is an orchestra's own
classical concert source rather than a mixed venue calendar, but that cannot
substitute for direct, repeatable access.

## What would unblock implementation

Allowlisting the crawler infrastructure's public IP, relaxing the NinjaFirewall
rule for read-only public pages and WordPress REST endpoints, or providing an
accessible first-party calendar/API feed would permit the concert archive and
current season to be inspected and parsed. Once access is restored, the network
requests should be rechecked before falling back to parsing `/concerts/`, its
detail pages, and `/past-concerts/`.
