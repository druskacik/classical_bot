<!-- crawler-factory-metadata
{"url":"https://www.johnfahey.com/","geographic_scope":"country","country_code":"US","reason_code":"no_current_events","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# Crawler blocked: no in-scope concerts

## Original URL

https://www.johnfahey.com/

The source is a United States-based archival site devoted to American guitarist
John Fahey. It does not publish a current event calendar, and the historical
performances exposed by its archive are not in scope for this project's
classical-event definition.

## Investigation

- Playwright network inspection of the home page, archive, and concert-review
  index found only static HTML and image requests. There were no XHR/fetch API,
  JSON calendar, or other structured event endpoints to reconstruct.
- The site has no first-party genre, category, discipline, event-type, series,
  or tag filters. It also has no sitemap at `/sitemap.xml` (HTTP 404).
- HTML navigation was inspected through **The Site Archives** and its **Concert
  Reviews** index. The index exposes a small historical set of reviews rather
  than a paginated event feed. Concrete dated examples include Berkeley on
  1998-04-11, Chicago on 1999-05-01, London on 1999-10-02, and Tokyo on
  2000-10-27. A New York entry does not expose a usable date on the index.
- Representative detail HTML was checked for the Berkeley, Chicago, London,
  and Tokyo reviews, plus the 2001 Freight & Salvage tribute page. The pages
  describe blues, alternative/experimental electric guitar, improvisation,
  and “American style acoustic guitar.” Although one retrospective Chicago
  review calls some compositions classically inspired, the advertised
  performances themselves do not establish classical performance practice or
  another eligible event type under `prompts/event_inclusion_guidance.mustache`.
- The archive also links articles, tablature, interviews, recordings, and photo
  pages. These are not concrete in-scope performance occurrences.

## Why implementation is not possible

A crawler could mechanically parse a few historical review pages, but it would
return only nonclassical performances (and some non-event editorial material).
There is therefore no appropriate classical feed to upload and no candidate
feed whose recurring publication warrants potential-event classification.

## What would unblock implementation

Implementation can be retried if the site adds a current or archived calendar
containing concrete classical, contemporary-art-music, classical-crossover, or
other project-eligible performances with defensible dates, venues, and cities.
A first-party structured feed or stable HTML event index would make that source
practical to crawl.
