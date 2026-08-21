<!-- crawler-factory-metadata
{"url":"https://www.espresyon.org/pages/frandsly_mere","geographic_scope":"country","country_code":"US","reason_code":"wrong_source","attempted_at":"2026-08-21","retry_after":null}
-->

# Crawler blocked: unrelated source

## Original URL

https://www.espresyon.org/pages/frandsly_mere

## Why a crawler cannot currently be implemented

The supplied URL is not a concert source. It resolves to `frandsly_mere`, a
quarterly catalogue raisonné for the visual artist Frandsly Mere, published by
the Fort Lauderdale, Florida business Espresyon. The page documents visual art,
intellectual property, provenance, and publishing information; it contains no
concert calendar or concrete performance occurrences.

The site's complete Shopify sitemaps expose only this publication page, contact
and privacy pages, three visual-art/merchandise products, two store collections,
and an empty news-blog index. No current or archived concert listings are
present. The resolved organization is therefore unrelated to the requested
classical-concert source, so `wrong_source` is more accurate than an empty-event
or access-failure status.

## Investigation performed

- Opened the supplied page with Playwright and inspected its rendered HTML and
  navigation. The page identifies itself as a digital serial/catalogue raisonné
  based in Fort Lauderdale, Florida, USA.
- Inspected page network traffic before considering HTML parsing. The only
  structured endpoint observed was Shopify's Storefront GraphQL consent query;
  there was no events, calendar, ticketing, or concert API request.
- Inspected the first-party Shopify sitemap index and its page, product,
  collection, and blog child sitemaps. They list no event or concert URLs and no
  historical event archive.
- Checked the site's visible catalogue structure. It is a small visual-art and
  merchandise storefront, with no first-party genre, category, discipline,
  event-type, series, or tag filters applicable to performances. Consequently,
  there were no filter values or pagination behavior to test.

## What would unblock implementation

Provide the intended concert-presenting organization's URL or a first-party
calendar/API endpoint containing concrete performance dates, venues, and
locations. The present domain would become viable only if it begins publishing
such listings in its HTML, Shopify data, or another discoverable first-party
feed.
