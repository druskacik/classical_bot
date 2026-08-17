<!-- crawler-factory-metadata
{"url":"https://wjcms.org/","geographic_scope":"country","country_code":"US","reason_code":"access_blocked","attempted_at":"2026-08-17","retry_after":"2026-09-16"}
-->

# Access blocked

## Original URL

https://wjcms.org/

The source is the West Jersey Chamber Music Society, a classical-only concert
organization based in Moorestown, New Jersey, United States. The canonical site
currently replaces all attempted pages and machine-readable endpoints with a
SiteGround robot challenge. The challenge requires an image CAPTCHA and cookies,
so a production crawler cannot retrieve a concert catalogue reliably.

## Investigation performed

- Opened the canonical home page in Playwright and inspected its network
  requests. Navigation was redirected to `/.well-known/sgcaptcha/` and then
  `/.well-known/captcha/`; the only non-document requests were CAPTCHA assets.
  No concert API, JSON feed, or application request was exposed.
- Inspected the rendered challenge in Playwright. It requires manually retyping
  an image CAPTCHA before continuing, which cannot be part of an unattended
  crawler.
- Requested the WordPress REST API at `/wp-json/` and the WordPress sitemap at
  `/wp-sitemap.xml`. Both returned HTTP 202 HTML containing a meta-refresh to
  the same robot challenge rather than JSON or XML.
- Requested `/robots.txt` and likely HTML listing routes `/concerts/` and
  `/events/`. Every route returned the same HTTP 202 challenge stub, so neither
  HTML parsing nor route discovery is currently viable.
- Checked public search indexing to verify the organization and source content.
  Indexed snippets show a 2025–2026 classical chamber-music season and concrete
  performances, but a search cache is neither a complete nor stable first-party
  pagination/archive interface and cannot support a universal crawler.

No applicable first-party genre, category, discipline, event-type, series, or
tag filters could be tested because the server blocks access before the site or
its API loads. Consequently, pagination and date-range persistence could not be
evaluated. No feed was selected and no upload target is applicable while access
is blocked.

## What would unblock implementation

Any stable, unattended first-party access path would unblock the crawler, for
example allowlisting the production crawler IP, disabling the CAPTCHA for the
public concert and WordPress API routes, or providing a public JSON, RSS, ICS,
or other documented events feed. Once accessible, the listing/API pagination,
archives, detail pages, and any first-party taxonomy filters must be inspected
before selecting the feed and upload target.
