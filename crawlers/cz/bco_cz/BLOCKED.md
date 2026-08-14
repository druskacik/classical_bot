<!-- crawler-factory-metadata
{"url":"https://www.bco.cz/","country_code":"CZ","reason_code":"wrong_source","attempted_at":"2026-07-29","retry_after":null}
-->

# Crawler blocked

## Original URL

https://www.bco.cz/

## Why the crawler cannot currently be implemented

The supplied domain no longer exposes a classical music website or any
concert listings. It currently serves the website of Business centrum
Ocelářská, an office-rental business in Prague. Creating a crawler from the
current content would therefore produce no classical concert records.

## Approaches attempted

- Opened the original URL in Playwright and inspected the rendered page,
  navigation, links, and page text.
- Inspected the browser network requests for an event API or another
  structured data source. The non-static traffic consisted only of Google
  Analytics, DoubleClick, and reCAPTCHA requests; no concert API or feed was
  present.
- Inspected the live HTML and client-side links. They contain office rental,
  parking, meeting-room, and contact content, with no concerts, dates,
  programmes, composers, or works.
- Tried likely concert listing routes (`/koncerty` and `/program`); both
  resolve to the site's 404 page.

## What would unblock implementation

Implementation can proceed when the original domain again exposes concert
listings, or when a confirmed current URL/API for the intended classical
music organization is supplied.
