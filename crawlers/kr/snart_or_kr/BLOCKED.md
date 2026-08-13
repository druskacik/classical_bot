<!-- crawler-factory-metadata
{"url":"https://www.snart.or.kr/","geographic_scope":"country","country_code":"KR","reason_code":"access_blocked","attempted_at":"2026-08-13","retry_after":"2026-09-12"}
-->

# Crawler blocked

## Original URL

https://www.snart.or.kr/

## Why implementation is currently blocked

The canonical website is protected by a web application firewall that blocks
the crawler environment before the application is served. The response says
that the request/response violates the site's web-firewall security policy and
includes the crawler's detected proxy address. This occurs for the homepage,
the integrated search, the programme area, and individual event URLs.

Because the block is returned before the application loads, there is no stable
first-party listing response, API response, or detail HTML that a production
crawler can fetch and validate. Search-engine results demonstrate that the site
does publish both current and archived performances, but a search index is not
a complete or reliable first-party feed and cannot support a universal crawler.

## Approaches attempted

- Opened both `https://www.snart.or.kr/` and the redirecting bare-domain URL in
  Playwright. Both resolved to the canonical host and returned the firewall
  block page.
- Opened the site's integrated-search path and programme-list path directly in
  Playwright. Both returned the same firewall block before application content
  loaded.
- Inspected Playwright network traffic for API, AJAX, JSON, event, programme,
  schedule, and calendar requests. No application request was available because
  only the firewall response loaded.
- Investigated indexed first-party pages and found the concrete detail pattern
  `/main/prex/prefer/view.do?prfr_exhb_sn=...`, as well as the integrated search
  path `/main/search/list.do`. Direct access to these pages remains blocked.
- Checked indexed representative records. The source is a mixed performing-arts
  venue and exposes first-party labels such as `기획 클래식` (planned/classical),
  `대관 클래식` (rental/classical), `기획 연극` (planned/theatre), `기획 어린이공연`
  (planned/children's performance), and `대관 국악` (rental/Korean traditional
  music). Without access to the listing controls or their requests, the exact
  filter values, pagination persistence, archive coverage, and adjacent-category
  coverage cannot be verified.

## What would unblock implementation

Allowing the crawler/production egress address through the site's firewall, or
providing a reachable first-party API/feed containing listing pagination and
event details, would allow the filters and archive ranges to be validated and a
crawler to be implemented. Given that this is a mixed source and the accessible
evidence is insufficient to prove a comprehensive in-scope filtered feed, an
event feed would need to use the `potential` upload target unless verified
first-party filters can safely cover all eligible classical, opera, ballet,
contemporary-art-music, crossover, soundtrack, musical, and family performance
categories without contamination.
