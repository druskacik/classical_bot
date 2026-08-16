<!-- crawler-factory-metadata
{"url":"https://www.ticketleap.com/","geographic_scope":"country","country_code":"US","reason_code":"wrong_source","attempted_at":"2026-08-16","retry_after":null}
-->

# Crawler blocked: supplied URL is not the Mankato Symphony source

## Original URL

https://www.ticketleap.com/

## Why a crawler cannot currently be implemented

The supplied URL is Ticketleap's generic event-ticketing platform marketing site, not a Mankato Symphony Orchestra organizer calendar. It contains no concert listing or event-discovery feed. The likely legacy organizer hostname, `https://mankatosymphony.ticketleap.com/`, now redirects to the same generic homepage, so it cannot identify an organizer or expose current or archived Mankato Symphony events.

Ticketleap is a mixed, multi-organizer platform. Without a valid Mankato Symphony organizer URL or stable first-party organizer identifier, crawling a broad Ticketleap feed would be both unrelated to the assigned source and impossible to filter comprehensively to the project's inclusion scope.

## Approaches attempted

- Opened the supplied URL with Playwright and inspected its rendered content, links, forms, and network requests. The page is a marketing site for event organizers and exposes no event API, search endpoint, category/genre filters, date-range controls, or pagination.
- Inspected first-party network traffic for API, event, search, and ticket requests. No structured event-data request was made; only site assets and Cloudflare challenge traffic were present.
- Tested `https://mankatosymphony.ticketleap.com/` as the organizer hostname implied by the assigned crawler directory. It redirects to `https://www.ticketleap.com/`.
- Checked Ticketleap's current `events.ticketleap.com` organizer/listing URL pattern. The generic listing route contains no upcoming events and does not provide a Mankato Symphony organizer identity.
- Searched indexed Ticketleap pages for Mankato Symphony and Mankato Symphony Orchestra event or organizer URLs. No first-party Ticketleap organizer/archive page for this organization was discoverable.

No applicable first-party genre, category, discipline, event-type, series, or tag filters were exposed, so there were no filter values or pagination behavior to validate.

## What would unblock implementation

Provide a live Ticketleap organizer page, a concrete Ticketleap event URL belonging to Mankato Symphony Orchestra (from which its stable organizer identifier can be recovered), or an official first-party organizer API/feed URL. The orchestra's separate official website is live, but substituting it would change the assigned source and canonical URL.
