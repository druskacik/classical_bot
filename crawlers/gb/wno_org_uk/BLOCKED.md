<!-- crawler-factory-metadata
{"url":"https://wno.org.uk/","geographic_scope":"country","country_code":"GB","reason_code":"access_blocked","attempted_at":"2026-08-14","retry_after":"2026-09-13"}
-->

# Crawler blocked by Cloudflare

The original URL is https://wno.org.uk/. Welsh National Opera is based in the
United Kingdom and tours within the UK, so the resolved crawler geography is
GB rather than multi-country.

A working crawler cannot currently be implemented because Cloudflare returns
an HTTP 403 interactive challenge instead of the site's pages to both the
browser and ordinary HTTP clients. The challenge does not resolve in the
browser. Consequently, a production crawler cannot discover all occurrences
or reliably extract their dates, cities, venues, and descriptions.

## Approaches attempted

- Loaded the homepage, the full What's On page, and the first-party Opera page
  with Playwright. Network inspection exposed only the blocked document and
  Cloudflare challenge/Turnstile requests; no concert API request was made.
- Tested the canonical apex and `www` hosts with both Playwright and direct
  browser-like HTTP requests. Both returned the same HTTP 403 challenge.
- Probed the current listing and category HTML routes `/whats-on/`,
  `/whats-on/start/`, `/whats-on/productions/`, and `/whats-on/concerts`, plus
  a representative production detail at `/whats-on/bc-or`. None returned
  parseable HTML.
- Probed `/robots.txt`, `/sitemap.xml`, `/sitemap_index.xml`, likely API roots,
  and an AJAX variant of the listing. All were protected by the same challenge,
  so no structured API or alternate discovery feed could be reconstructed.
- Checked current public search indexing. It confirms concrete upcoming opera
  and concert performances and also confirms that the site retains production
  archives. Indexed first-party category routes include exact values `Opera`
  (`/whats-on/productions/`), `Concerts` (`/whats-on/concerts`), `Family events`
  (`/whats-on/family-events/`), `Free events & talks`
  (`/whats-on/free-events-talks`), and `Take part` (`/whats-on/take-part`). The
  Opera, Concerts, and relevant Family events routes contain in-scope examples;
  Free events & talks and Take part contain contamination such as exhibitions
  and participatory activity. Because the source could not be loaded, stable
  filter identifiers, pagination/date-range persistence, completeness across
  those categories, and occurrence markup could not be verified. Search-index
  snippets are incomplete and are not a suitable universal production source.

## What would unblock implementation

Implementation would be unblocked by allowlisted non-interactive access to the
public listings and detail pages, successful browser passage through the
Cloudflare challenge, or a stable public JSON, XML, RSS, or iCalendar feed that
is exempt from the challenge. Once access is available, the first-party Opera,
Concerts, and Family events feeds should be tested together across pagination
and date ranges, with adjacent categories checked for other eligible music
performances, before deciding between direct classical upload and potential
classification.
