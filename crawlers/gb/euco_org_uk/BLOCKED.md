<!-- crawler-factory-metadata
{"url":"https://euco.org.uk/","geographic_scope":"country","country_code":"GB","reason_code":"no_current_events","attempted_at":"2026-08-21","retry_after":"2026-09-20"}
-->

# No scrapeable concert source

The supplied URL is the official website of the European Union Chamber Orchestra, a UK-based orchestra. The site is accessible, but it currently exposes no current concert listings and no structured archive of individual past concerts from which the required event fields can be recovered.

## Investigation performed

- Loaded the home page in Playwright and inspected its browser network requests. No concert or event API request was made; the page is a static, single-page organization profile containing general history, reviews of past performances, recordings, education material, and contact information.
- Inspected the WordPress REST API. Its public content types are posts, pages, attachments, reusable blocks, and Divi projects; there is no event or concert type. The posts and projects collections both contain zero records. The pages collection contains only Home, Sitemap, Terms & Conditions, and Privacy Policy.
- Checked `robots.txt`, the declared XML sitemap index, the WordPress sitemap, and the human-readable sitemap. They expose no event pages or concert archive. The available sitemap content consists only of the same small set of static pages, plus empty post/category/tag indexes.
- Inspected the rendered home-page HTML/text and navigation for event, concert, diary, archive, genre, category, discipline, event-type, series, and tag feeds. No applicable first-party filters or paginated event feed exist. The isolated reviews dated July and October 2024 do not provide exact performance dates and are not reusable concert records.

## What would unblock implementation

EUCO would need to publish a calendar or archive containing concrete concert occurrences with exact dates and defensible venues/cities, either as HTML pages or through a stable first-party API/feed. An official external ticketing or calendar feed linked by EUCO and containing those fields would also be sufficient.
