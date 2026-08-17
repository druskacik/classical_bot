import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://trinitychurchnyc.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events-search')
SOURCE = 'Trinity Church Wall Street'
CITY = 'New York'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_pages(session):
    """Yield every page of Trinity's first-party Music event feed."""
    url = EVENTS_URL
    params = {'event_type[22]': '22'}
    seen = set()
    while url and url not in seen:
        seen.add(url)
        soup = get_soup(session, url, params=params)
        yield soup
        next_link = soup.select_one('a[rel="next"], .pager__item--next a')
        url = urljoin(url, next_link.get('href')) if next_link else None
        params = None


def normalized_venue(card):
    locations = [clean_text(node) for node in card.select('.teaser-event-listing__location')]
    locations = [value for value in locations if value and value.lower() != 'online']
    if not locations:
        return None
    venue = locations[0]
    venue = re.sub(r',?\s*Livestream$', '', venue, flags=re.I).strip(' ,')
    return venue or None


def listing_record(card):
    link = card.select_one('.teaser-event-listing__title a[href]')
    title = clean_text(link)
    date_text = clean_text(card.select_one('.teaser-event-listing__date'))
    venue = normalized_venue(card)
    if not link or not title or not date_text or not venue:
        return None
    try:
        event_date = datetime.strptime(date_text, '%A, %B %d, %Y').date().isoformat()
    except ValueError:
        return None

    time_text = clean_text(card.select_one('.teaser-event-listing__time'))
    try:
        time_from = datetime.strptime(time_text, '%I:%M %p').strftime('%H:%M')
    except ValueError:
        time_from = None

    description_parts = [
        clean_text(card.select_one('.teaser-event-listing__sub-title')),
        clean_text(card.select_one('.teaser-event-listing__summary')),
    ]
    description = '\n\n'.join(part for part in description_parts if part) or None
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, link.get('href')),
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(soup, fallback=None):
    heading = soup.find(
        ['h2', 'h3'], string=lambda value: value and 'About the Event' in value
    )
    if heading:
        container = heading.parent
        parts = [clean_text(node) for node in container.find_all(['p', 'ul', 'ol'], recursive=False)]
        description = '\n\n'.join(part for part in parts if part)
        if description:
            return description
    return fallback


def fetch_detail(session, record):
    soup = get_soup(session, record['url'])
    record['description'] = detail_description(soup, record['description'])
    return record


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for soup in listing_pages(session):
        for card in soup.select('article.teaser-event-listing'):
            record = listing_record(card)
            if record:
                records.append(record)

    detailed = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_detail, session, record): record for record in records}
        for future in as_completed(futures):
            record = futures[future]
            try:
                detailed.append(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                detailed.append(record)

    return sorted(
        detailed,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class TrinityChurchNycOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='trinitychurchnyc_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    TrinityChurchNycOrgCrawler().run()


if __name__ == '__main__':
    main()
