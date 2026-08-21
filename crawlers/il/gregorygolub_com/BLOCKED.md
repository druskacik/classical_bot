<!-- crawler-factory-metadata
{"url":"https://gregorygolub.com/","geographic_scope":"country","country_code":"IL","reason_code":"no_current_events","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# Gregory Golub crawler blocked

## Original URL

https://gregorygolub.com/

## Why a crawler cannot currently be implemented

The website is the personal site of Israeli jazz pianist and composer Gregory
Golub. It does not publish a concert calendar, event listing, or archive from
which valid concert records can be built. The only concrete dated performance
mentions found are brief biographical references to solo programs in Tel Aviv
on October 1, 2021 and May 20, 2023. Neither mention identifies a venue, and the
surrounding material does not establish that these jazz-oriented programs meet
the project's classical or qualifying crossover scope. Returning them would
therefore require inventing a required venue and making an unsupported scope
classification.

## Approaches attempted

- Inspected the homepage network traffic with Playwright. No event API or
  structured concert feed was requested; the only first-party JSON endpoints
  observed were unrelated Bandzoogle member/cart endpoints.
- Retrieved and inspected `robots.txt` and the complete `sitemap.xml`. The
  sitemap lists home, biography, media, press, reviews, and project/blog pages,
  but no event or calendar page.
- Tested the conventional Bandzoogle paths `/shows`, `/events`, and `/calendar`;
  all returned HTTP 404 pages.
- Inspected the rendered text and HTML content of every sitemap page for dates
  and concert, recital, performance, tour, and show references. This found
  biographical or recording-related prose only, not scrapeable event entries
  with the required venue and scope evidence.
- No first-party genre, category, discipline, event-type, series, or tag filters
  exist because the site exposes no event feed or archive.

## What would unblock implementation

A first-party calendar or archive containing concrete performances with dates,
venues, and cities would make a crawler possible. For this mixed jazz and
genre-crossing artist source, the listings would also need stable first-party
category metadata or enough event detail to send a comprehensive candidate feed
through potential-event classification.
