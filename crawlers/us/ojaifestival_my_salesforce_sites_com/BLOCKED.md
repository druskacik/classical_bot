<!-- crawler-factory-metadata
{"url":"https://ojaifestival.my.salesforce-sites.com/","geographic_scope":"country","country_code":"US","reason_code":"no_parseable_source","attempted_at":"2026-08-16","retry_after":"2026-09-15"}
-->

# Crawler blocked: no concert source on assigned host

## Original URL

https://ojaifestival.my.salesforce-sites.com/

The host belongs to the US-based Ojai Music Festival, but it is a Salesforce donation site rather than the organization's concert calendar. The bare URL redirects to `/donate/` and currently returns a Salesforce “Login is required to access this URL” error. A publicly indexed donation-form URL with a `dfId` parameter loads successfully, but contains only fundraising and payment fields. It exposes no current concerts, past-concert archive, event dates, programme details, or event detail URLs from which valid crawler records could be built.

## Investigation performed

- Loaded the root URL with Playwright and inspected its redirect, rendered HTML, accessibility tree, console, and network requests.
- Inspected the successful public donation URL `https://ojaifestival.my.salesforce-sites.com/donate/?dfId=a0n2S00000mKOpCQAW`.
- Reviewed all first-party network requests from that page. They were Visualforce framework files, donation-form assets, images, payment JavaScript, and reCAPTCHA; there was no event API, structured concert response, pagination request, genre/category filter, or schedule feed.
- Requested `robots.txt` and `sitemap.xml`. The Salesforce robots file disallows general crawling and reveals no content paths; the sitemap path returns “Site file not found exception.”
- Checked indexed URLs for the assigned hostname. The only discoverable public page was the same donation form. Concert schedules and archives are published separately on `www.ojaifestival.org`, which is outside the exact assigned source.
- Considered HTML parsing, but both accessible HTML variants contain only a Salesforce error page or donation content, so there are no concert records to parse.

## What would unblock implementation

Provide an event-listing or archive URL on this Salesforce host, including any required stable public query parameter, or reassign the crawler to the canonical Ojai Music Festival website (`https://www.ojaifestival.org/`), where festival schedules and past-festival pages are published.
