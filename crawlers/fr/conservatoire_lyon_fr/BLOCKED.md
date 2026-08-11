<!-- crawler-factory-metadata
{"url":"https://www.conservatoire-lyon.fr/","geographic_scope":"country","country_code":"FR","reason_code":"no_current_events","attempted_at":"2026-08-11","retry_after":"2026-09-10"}
-->

# Conservatoire de Lyon crawler blocked

## Original URL

https://www.conservatoire-lyon.fr/

The source is the Conservatoire à Rayonnement Régional de Lyon, so the resolved
geography is France (`FR`). The organization is based in Lyon and the site does
not present itself as a genuinely multi-country event source.

## Why a crawler cannot currently be implemented

The first-party agenda currently publishes no scrapeable events. Its unfiltered
“Tout l’agenda” view reports no results, and the site exposes no historical
agenda items that could provide a stable parser fixture. Creating `main.py`
would therefore produce an untested scraper based on guessed markup rather than
a working universal crawler.

The site is mixed: it covers music, dance, and theatre. Its agenda form exposes
a `thematique` selector, but the only available value is `all`; there are no
first-party music, classical, discipline, genre, series, or event-type values to
test or combine. Consequently, if events return before a reliable filter does,
the appropriate feed would be the unfiltered agenda with
`upload_target="potential"`, not a direct classical upload.

## Approaches attempted

- Inspected the initial page and agenda with Playwright, including all network
  requests. The agenda is rendered in the initial HTML and makes no event API or
  AJAX request.
- Tested the stable agenda query values `periode=` (Tout l’agenda),
  `periode=mois`, and `thematique=all&periode=`. Each returned the same empty
  result; there was no pagination to verify.
- Inspected the agenda form. `thematique=all` is its only thematic value; the
  period values are `aujourdhui`, `weekend`, `semaine`, `mois`, and the empty
  value used for the full agenda.
- Inspected the public WordPress REST API, including its post types, taxonomies,
  and search endpoint. It exposes no agenda/event post type or applicable event
  taxonomy from which structured concert data can be reconstructed.
- Inspected `/agenda/feed/`; the first-party RSS feed is valid but contains zero
  items. The XML sitemaps likewise expose no agenda or event archive.
- Inspected the server-rendered HTML for event/detail links and concrete event
  cards. None were present, including in the full-agenda query.

## What would unblock implementation

Publication of at least a few concrete agenda entries (preferably including
music and adjacent dance/theatre examples) would provide the markup and detail
pages needed to implement and validate extraction. A documented or discoverable
first-party event API/feed containing current or archived items would also
unblock the crawler. The source should be retried after the new cultural season
has been published.
