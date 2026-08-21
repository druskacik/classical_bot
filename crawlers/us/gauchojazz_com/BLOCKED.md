<!-- crawler-factory-metadata
{"url":"https://gauchojazz.com/","geographic_scope":"country","country_code":"US","reason_code":"no_current_events","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# Gaucho Jazz crawler blocked

## Original URL

https://gauchojazz.com/

## Why a crawler cannot currently be implemented

The source is the official site of Gaucho, a San Francisco Bay Area band whose
published biography describes its music as gypsy jazz, New Orleans swing,
blues, ragtime, country, and rock. These are outside the project's classical
event scope unless a specific performance has substantial classical forces or
repertoire. No such performance was found in either the current schedule or
the available archive.

The eight upcoming records are ordinary Gaucho appearances at Comstock Saloon,
Brenda's, and Club Deluxe in San Francisco. The historical feed likewise
contains jazz appearances and also has clear artist-name collisions, including
unrelated punk/electronic mixed bills outside the United States. Scraping that
feed would therefore produce no in-scope records and would introduce known
nonclassical contamination even if routed through potential-event review.

## Approaches attempted

- Inspected the rendered home page and dedicated `/gigs-shows` page with
  Playwright.
- Inspected browser network traffic before relying on HTML parsing. The gigs
  page loads structured data from the Bandsintown endpoint
  `https://rest.bandsintown.com/artists/Gaucho/events` with
  `app_id=squarespace-gauchojazz`.
- Tested the endpoint's exact `date` values `upcoming`, `past`, and `all`.
  They returned 8, 1,516, and 1,524 records respectively. The combined result
  spans 2002-03-10 through 2026-09-11 and equals the past plus upcoming counts,
  so the values cover the available date range without a pagination mechanism.
- Looked for first-party genre, category, discipline, event-type, series, and
  tag filters. Neither the Squarespace page nor the Bandsintown request exposes
  an applicable filter; only the artist and date-range parameters are present.
- Inspected representative current records and historical records, including
  a detailed 2016 Gaucho description. They establish jazz/swing programming,
  not eligible classical or classical-crossover performance.

## What would unblock implementation

The site would need to begin publishing concrete performances that meet the
project's classical inclusion guidance—for example a collaboration where an
orchestra or classical ensemble is a substantial billed participant—and expose
enough event detail to identify those performances reliably. A stable
first-party genre/category filter would help; otherwise a future implementation
would need to send an appropriately bounded candidate feed to potential-event
classification.
