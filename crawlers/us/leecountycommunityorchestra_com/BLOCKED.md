<!-- crawler-factory-metadata
{"url":"https://www.leecountycommunityorchestra.com/","geographic_scope":"country","country_code":"US","reason_code":"wrong_source","attempted_at":"2026-08-18","retry_after":null}
-->

# Crawler blocked: domain repurposed

## Original URL

https://www.leecountycommunityorchestra.com/

## Why a crawler cannot currently be implemented

The supplied domain no longer serves the Lee County Community Orchestra. Its
HTML identifies itself as an online casino page and immediately redirects users
with JavaScript to `https://www.aahyz.com/m/register`, an unrelated registration
site. There are no orchestra concert listings, archives, or event detail pages
at the supplied source, so a production crawler cannot locate any concerts or
produce valid records.

The resolved geography remains United States (`US`) because the requested
source was the Lee County Community Orchestra. The replacement site's content
does not change the intended source's geography.

## Approaches attempted

- Opened the original URL in Playwright and inspected its navigation and network
  traffic. The original document returned HTTP 200 and then loaded the unrelated
  `aahyz.com/m/register` document.
- Inspected non-static browser requests for a reconstructable API. The only
  dynamic endpoints belonged to the replacement registration/gaming site (for
  example, its `/wps/system/templates` endpoint); no concert, calendar, event,
  category, genre, archive, or orchestra API was exposed.
- Requested the original URL directly and inspected its HTML. The response is a
  1.3 KB JavaScript redirect page with an online-casino title and no concert
  content, links, structured data, or archive navigation.
- Checked both the API/network and HTML approaches requested for crawler
  investigation. Neither exposes a first-party concert source. Consequently,
  there are no applicable first-party filters or filter values to test, and no
  pagination or date-range behavior to validate.

## What would unblock implementation

Implementation would require the orchestra to restore its official website at
the supplied domain or provide another stable first-party calendar, API, RSS,
iCalendar, or archive source containing concrete performances and their dates,
venues, cities, and detail descriptions. Any replacement source would also need
its ownership and canonical URL verified before a crawler could be built.
