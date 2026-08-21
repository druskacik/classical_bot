<!-- crawler-factory-metadata
{"url":"https://www.jounisomero.com/","geographic_scope":"country","country_code":"FI","reason_code":"no_parseable_source","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# Crawler blocked: no parseable concert source

## Original URL

https://www.jounisomero.com/

## Why a crawler cannot currently be implemented

Jouni Somero's official Finnish website does not publish a concert calendar or
event detail pages. Its concert section points to `www.finnconcert.fi` and says
that the gig calendar is available on the Facebook page "FinnConcert Oy".

The only concrete performance occurrence visible on the supplied site is a
retrospective news note saying that Somero's 3,500th concert took place in Raahe
on 25 February 2026. It gives no venue, so it cannot produce a valid record under
the crawler contract. Other dates on the site describe recordings, releases,
reviews, or general programme products rather than concert occurrences.

## Approaches attempted

- Inspected the homepage and its browser network requests with Playwright. No
  event API, JSON feed, calendar request, pagination, date-range endpoint, or
  first-party genre/category filter was exposed.
- Inspected the rendered HTML, links, forms, iframes, and scripts. There are no
  hidden event records or concert-detail links; the site's own search returned
  no results for `konsertti`.
- Followed the first-party `www.finnconcert.fi` link. That site contains artist
  profiles and bookable programme descriptions, but no dated performance feed
  or archive.
- Attempted to open the stated FinnConcert Oy Facebook calendar. Facebook
  redirected the browser to its login page, leaving no stable public HTML or
  API response that a production crawler can consume.

No applicable first-party event filters exist, so pagination and filter
persistence could not be tested. The organization is based in Finland and the
available evidence concerns its Finnish operation, hence the resolved country
code is `FI` rather than a multi-country scope.

## What would unblock implementation

A publicly accessible, stable calendar on jounisomero.com or finnconcert.fi
with concrete dates, venues, and cities would unblock a crawler. A documented
or anonymous first-party Facebook/Graph API feed providing the same fields
would also be sufficient.
