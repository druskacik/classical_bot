<!-- crawler-factory-metadata
{"url":"https://operanb.ro/","geographic_scope":"country","country_code":"RO","reason_code":"access_blocked","attempted_at":"2026-08-15","retry_after":"2026-09-14"}
-->

# Opera Națională București crawler blocked

Original URL: https://operanb.ro/

The source is the Romanian Opera Națională București, based in Bucharest,
Romania. Its calendar is publicly visible in an interactive browser, but direct
HTTP clients receive a JavaScript browser-verification page instead of calendar
or performance HTML. Consequently, a crawler using the repository's available
production HTTP interfaces cannot currently retrieve records reliably.

## Investigation performed

- Inspected browser network traffic on the home page, calendar, and performance
  detail pages. No calendar/event API, GraphQL request, or AJAX data feed was
  made; the calendar is rendered into the initial HTML response.
- Checked the public WordPress REST type endpoint. The performance/calendar
  content is not exposed as a public REST post type.
- Verified the stable calendar query format
  `https://operanb.ro/calendar/?luna=MM&anul=YYYY` in Playwright, including a
  populated past month (`luna=06&anul=2026`) and adjacent-month navigation.
- Inspected representative performance pages, including `Gala Opera Italiana`
  and `Tosca`. Detail pages expose occurrence dates/times and, where available,
  long cast/programme-related content in server-rendered HTML.
- Tested ordinary `requests` and `curl_cffi` with Chrome impersonation. Both
  received the site's `Please wait while your request is being verified...`
  JavaScript challenge rather than calendar HTML.
- Attempted the challenge's generated verification request with an HTTP session;
  it did not establish access, confirming that replaying the form without a real
  browser is not a viable production approach.

## Feed scope and filtering

The calendar exposes only the generic first-party label `Evenimente`; no genre,
discipline, event-type, series, or tag filter is present in the calendar query or
network traffic. There is no pagination: navigation is by the stable `luna` and
`anul` month/year values, which persisted when moving across past months.

The unfiltered calendar contains opera, ballet, concerts, children's works, and
musicals, but also out-of-scope or ambiguous entries. For example, June 2026
included a book launch alongside opera and ballet performances. Therefore, if
access becomes available, the complete monthly candidate calendar should use
`upload_target="potential"`; it must not upload the unfiltered feed directly as
classical.

## What would unblock implementation

Any one of the following would allow a working crawler:

- first-party removal or allow-listing of the JavaScript verification for the
  crawler runtime;
- a documented/public calendar API or export feed;
- a repository-approved browser runtime and browser automation dependency in
  the production `classical-bot` image; or
- a stable, supported verification mechanism usable by the existing HTTP
  clients.

