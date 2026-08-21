<!-- crawler-factory-metadata
{"url":"https://www.faieviolin.com/","geographic_scope":"country","country_code":"US","reason_code":"no_parseable_source","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# Crawler blocked

## Original URL

https://www.faieviolin.com/

## Why a crawler cannot currently be implemented

The domain currently hosts the website of Northern California-based violinist Gail Hernández Rosa. Its `/tour` page contains a classical-performance calendar, but it does not expose enough consistent information to create valid concert records. The hand-authored entries omit every venue and city, most omit the year, and some use a month without a day. Many ticket links lead only to an ensemble homepage, calendar, or season overview rather than to the advertised occurrence. Inferring a home venue or city would also be incorrect because this is a touring performer's calendar.

Although one linked Eventbrite detail page supplies a complete event date and location, that is not representative of the feed. Adjacent linked sites either provide no structured event data, block ordinary HTTP retrieval, or aggregate several performances and locations. A crawler based on only the exceptional complete links would not locate all concerts; a crawler based on the calendar text would have to invent mandatory dates, venues, or cities.

The resolved geographic scope is the United States: the artist identifies herself as Northern California-based and the displayed engagements are with US organizations. This is a US touring-artist source, not a genuinely multi-country calendar.

## Approaches attempted

- Inspected browser network requests for the homepage and `/tour`. The only first-party dynamic requests were Squarespace census/analytics calls; there was no event, calendar, collection, GraphQL, or JSON API request to reconstruct.
- Inspected first-party page HTML and JSON-LD. The page has only website-level JSON-LD. Concerts are free-form `.sqs-html-content` blocks containing a date fragment, artist or organization, programme title, and an outbound ticket link.
- Checked the entire visible calendar, including past and future entries. It has no pagination, date-range control, archive, genre, category, discipline, event-type, series, or tag filters.
- Inspected representative outbound detail pages. The Eventbrite page for `Musings - In Concert` exposes Event JSON-LD with a 2026 date, time, Berkeley Piano Club, Berkeley, and US country code. By contrast, the Festival Opera and Corona del Mar Festival pages returned no Event JSON-LD, Philharmonia Baroque rejected direct HTTP retrieval, and several calendar entries link only to generic organizer or season pages.
- Considered HTML parsing with year rollover inference, but the displayed ordering is not strictly chronological and the page gives no explicit season/year heading. This cannot safely establish real calendar dates or locations for all records.

## What would unblock implementation

Any of the following would make a reliable crawler possible:

- first-party event records containing explicit year, venue, and city for each performance;
- a stable calendar or event API with those fields;
- event-specific links for every entry whose pages expose machine-readable dates and locations; or
- a consistent first-party archive/detail-page format with one concrete occurrence per record.

If the calendar is upgraded, the source appears classical-only and could likely use `upload_target="classical"`; no category filters would be needed. That decision must be rechecked against representative records at retry time.
