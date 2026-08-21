<!-- crawler-factory-metadata
{"url":"https://josephstrickland.blogspot.com/","geographic_scope":"country","country_code":"US","reason_code":"wrong_source","attempted_at":"2026-08-21","retry_after":null}
-->

# Wrong source

## Original URL

https://josephstrickland.blogspot.com/

## Why a crawler cannot currently be implemented

The supplied URL is the personal website of United States filmmaker, author, and soundtrack producer Joseph Strickland. It is not a classical-music concert presenter or event calendar. The complete published archive contains 51 posts from 2018 through 2025, all concerning the film *Dual Mania*, film-festival selections and awards, filmmaker interviews, soundtrack recordings, and retail or streaming availability. It contains no scrapeable classical concert occurrences, including in its archives.

The few dated event references are film screenings or film-festival activity, not live classical performances within the project's inclusion guidance. Soundtrack posts advertise recorded albums rather than concerts.

## Investigation performed

- Loaded the canonical HTTPS homepage with Playwright and inspected its navigation, visible posts, archive links, and older-post pagination.
- Inspected browser network requests. Blogger exposes a widget feed request and the standard Atom/JSON posts feed; no concert or event API was present.
- Queried the first-party Blogger JSON feed at `https://josephstrickland.blogspot.com/feeds/posts/default?alt=json&max-results=150`. It reported 51 total entries and returned all 51 in one response, so there was no additional feed page to inspect.
- Reviewed every feed title and searched all entry bodies for concert-related terms. Matches referred only to soundtrack sales/music, film premieres, and film festivals; none described an eligible classical concert.
- Inspected the HTML archive and its `Older Posts` pagination path as a fallback. It covers the same non-concert material exposed by the complete feed.
- Checked the site's available navigation areas, including Music and News. The source provides no first-party genre, category, discipline, event-type, series, or tag filters for concerts.

## What would unblock implementation

Provide the intended classical-music organization or concert-calendar URL. If this exact blog later begins publishing concrete classical concert occurrences with defensible dates, venues, and cities, it can be re-evaluated.
