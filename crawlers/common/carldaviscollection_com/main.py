import html
import re
from datetime import date
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://carldaviscollection.com/'
SOURCE = 'Carl Davis Collection'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/event'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
}

COUNTRY_CODES = {
    'China': 'CN',
    'France': 'FR',
    'Germany': 'DE',
    'Switzerland': 'CH',
    'USA': 'US',
}

VENUE_CITY_OVERRIDES = {
    'Shanghai International Dance Centre': 'Shanghai',
    'Shenzhen Grand Theatre': 'Shenzhen',
    'Staatstheater, Kassel': 'Kassel',
    'Holsten Halls, Neumünster': 'Neumünster',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(day_text, month_year_text):
    match = re.fullmatch(r'([A-Z]{3})\s+(20\d{2})', month_year_text.strip().upper())
    if not match or match.group(1) not in MONTHS:
        return None
    try:
        return date(
            int(match.group(2)), MONTHS[match.group(1)], int(day_text.strip())
        ).isoformat()
    except ValueError:
        return None


def parse_location(location_text, paragraphs, external_url):
    country_name = location_text.strip()
    city = None
    if ',' in country_name:
        city, country_name = [part.strip() for part in country_name.rsplit(',', 1)]

    country_code = COUNTRY_CODES.get(country_name)
    if not country_code:
        return None

    venue = None
    for candidate in paragraphs:
        for known_venue, known_city in VENUE_CITY_OVERRIDES.items():
            if known_venue.lower() in candidate.lower():
                venue, city = known_venue, known_city
                break
        if venue:
            break

    # This event links directly to Konzerthaus Dortmund's programme and the
    # first-party location is Dortmund, Germany, so the hall is unambiguous.
    if not venue and city == 'Dortmund' and urlparse(external_url).hostname == 'www.konzerthaus-dortmund.de':
        venue = 'Konzerthaus Dortmund'

    if not venue or not city:
        return None
    return venue, city, country_code


def parse_event_page(page_html, page_url, listing_links=None):
    soup = BeautifulSoup(page_html, 'html.parser')
    title_element = soup.select_one('body.single-event h1')
    if title_element is None:
        return None
    container = title_element.parent

    date_parts = container.select('div:first-child > div:first-child p')
    if len(date_parts) < 2:
        return None
    event_date = parse_date(clean_text(date_parts[0]), clean_text(date_parts[1]))

    metadata = container.select('h1 + div .text-sm.text-brand-dark-grey')
    if len(metadata) < 2:
        return None
    time_text = clean_text(metadata[0])
    location_text = clean_text(metadata[1])

    description_element = container.select_one('div.prose-sm')
    paragraphs = [clean_text(item) for item in description_element.select('p')] if description_element else []
    external_url = (listing_links or {}).get((html.unescape(clean_text(title_element)), event_date), '')
    location = parse_location(location_text, paragraphs, external_url)

    title = clean_text(title_element)
    if not title or not event_date or not location:
        return None
    venue, city, country_code = location

    return {
        'title': html.unescape(title),
        'date': event_date,
        'url': page_url,
        'time_from': time_text if re.fullmatch(r'(?:[01]?\d|2[0-3]):[0-5]\d', time_text) else None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_text(description_element) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class CarlDavisCollectionComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='carldaviscollection_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page = 1

        listing_links = {}
        try:
            listing_response = session.get(
                f'{SOURCE_URL}events-and-performances', timeout=45
            )
            listing_response.raise_for_status()
            listing_soup = BeautifulSoup(listing_response.text, 'html.parser')
            for heading in listing_soup.select('h3'):
                link = heading.find_parent('a', href=True)
                if link is None:
                    continue
                date_parts = link.select('div:first-child > div:first-child p')
                if len(date_parts) < 2:
                    continue
                listing_date = parse_date(clean_text(date_parts[0]), clean_text(date_parts[1]))
                if listing_date:
                    listing_links[(html.unescape(clean_text(heading)), listing_date)] = link['href']
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Carl Davis event listing',
                event='crawler_listing_fetch_failed',
                level='warning',
                url=f'{SOURCE_URL}events-and-performances',
                error_type=type(error).__name__,
                error_message=str(error),
            )

        while True:
            try:
                response = session.get(
                    API_URL,
                    params={'per_page': 100, 'page': page, '_fields': 'id,link,title'},
                    timeout=45,
                )
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Carl Davis event API',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            events = response.json()
            for event in events:
                page_url = event.get('link')
                if not page_url:
                    continue
                try:
                    detail_response = session.get(page_url, timeout=45)
                    detail_response.raise_for_status()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Carl Davis event page',
                        event='crawler_detail_fetch_failed',
                        level='warning',
                        url=page_url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                record = parse_event_page(detail_response.text, page_url, listing_links)
                if record:
                    records.append(record)

            total_pages = int(response.headers.get('X-WP-TotalPages', page))
            if page >= total_pages:
                break
            page += 1

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    CarlDavisCollectionComCrawler().run()


if __name__ == '__main__':
    main()
