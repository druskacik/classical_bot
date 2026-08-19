<!-- crawler-factory-metadata
{"url":"https://www.dso.hr/","geographic_scope":"country","country_code":"HR","reason_code":"access_blocked","attempted_at":"2026-08-19","retry_after":"2026-09-18"}
-->

# Access blocked

The assigned source is the official website of the Dubrovački simfonijski
orkestar (Dubrovnik Symphony Orchestra), a Croatian classical-music
organization. Its canonical URL is https://www.dso.hr/.

No reliable crawler can currently be implemented from this environment because
the site's MalCare firewall and browser-verification layer prevent access to
both the event HTML and the structured WordPress interfaces. A normal browser
request receives HTTP 403 with "Blocked because of Malicious Activities".
Requests that receive the JavaScript verification page cannot complete it: the
challenge's own same-origin verification endpoint returns HTTP 404, after which
the page reloads the challenge indefinitely.

The following approaches were attempted:

- Opened the home page and event calendar with Playwright and inspected all
  first-party network traffic. No event API request was reached before the
  firewall response.
- Requested the WordPress REST API root, content-type endpoint, and search
  endpoint (`/wp-json/`, `/wp-json/wp/v2/types`, and
  `/wp-json/wp/v2/search?per_page=5`). All were blocked before returning JSON.
- Requested the canonical host, bare domain, HTTP redirect, calendar page, and
  representative festival pages directly with browser-like and crawler user
  agents. Responses were either the MalCare 403 page or the JavaScript
  verification challenge rather than source content.
- Allowed the first-party JavaScript challenge to execute in Playwright and
  also reproduced its generated verification request. Its verification URL
  returned 404 and issued no access cookie.
- Checked publicly indexed first-party pages to confirm that concrete 2026
  concerts do exist. Search-index excerpts expose festival overview content,
  but search-engine excerpts are not a stable or complete first-party feed and
  therefore cannot support a universal crawler.

The organization is classical-only, so no genre/category filter would be
needed if access were restored. No applicable first-party genre, discipline,
event-type, series, or tag filter could be tested through pagination or date
ranges because neither the calendar nor API was accessible. Publicly indexed
festival pages show concrete concerts, but they are separate overview pages and
do not establish complete calendar coverage.

Implementation can be retried when the site permits server-side access to its
calendar or WordPress REST API, when its verification endpoint functions for
automated browsers, or when the organization publishes a stable first-party
calendar feed/API that is reachable from the production environment.
