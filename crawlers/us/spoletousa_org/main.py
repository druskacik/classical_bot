import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://spoletousa.org/'
LISTING_URL = 'https://spoletousa.org/shows/'
SOURCE = 'Spoleto Festival USA'
CITY = 'Charleston'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/151.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(session, url):
    last_error = None
    for attempt in range(3):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            return response.text
        except requests.RequestException as error:
            last_error = error
            log_message(
                'Request failed',
                event='crawler_request_failed',
                level='warning',
                url=url,
                attempt=attempt + 1,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    raise last_error


def parse_dates(value, year):
    text = re.sub(r'^Date:\s*', '', clean_text(value), flags=re.I)
    month = None
    dates = []
    for token in (part.strip() for part in text.split(',')):
        match = re.fullmatch(r'(?:(January|February|March|April|May|June|July|August|September|October|November|December)\s+)?(\d{1,2})', token, re.I)
        if not match:
            continue
        month = match.group(1) or month
        if not month:
            continue
        try:
            dates.append(datetime.strptime(f'{month} {match.group(2)} {year}', '%B %d %Y').date().isoformat())
        except ValueError:
            continue
    return dates


def detail_description(url):
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        soup = BeautifulSoup(get_page(session, url), 'html.parser')
    except requests.RequestException:
        return None
    overview = soup.select_one('.overview-section .module-content')
    return clean_text(overview) or None


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    soup = BeautifulSoup(get_page(session, LISTING_URL), 'html.parser')

    title_text = clean_text(soup.title)
    year_match = re.search(r'\b(20\d{2})\b', title_text)
    if not year_match:
        raise ValueError('Could not determine the festival season year')
    year = int(year_match.group(1))

    cards = []
    for tile in soup.select('.program-tile'):
        title_node = tile.select_one('.module-title')
        link = tile.select_one('.module-title a[href]')
        date_node = tile.select_one('.tile-dates')
        venue_node = tile.select_one('.tile-venues')
        title = clean_text(title_node)
        url = link.get('href', '').strip() if link else ''
        venue = clean_text(venue_node)
        venue = re.sub(r'^Venue:\s*', '', venue, flags=re.I).strip()
        dates = parse_dates(date_node, year)
        if not title or not url or not venue or not dates:
            continue
        cards.append({
            'title': title,
            'url': url,
            'venue': venue,
            'dates': dates,
            'description': clean_text(tile.select_one('.tile-blurb')) or None,
        })

    descriptions = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(detail_description, card['url']): card['url'] for card in cards}
        for future in as_completed(futures):
            description = future.result()
            if description:
                descriptions[futures[future]] = description

    records = []
    for card in cards:
        description = descriptions.get(card['url'], card['description'])
        for event_date in card['dates']:
            records.append({
                'title': card['title'],
                'date': event_date,
                'url': card['url'],
                'time_from': None,
                'venue': card['venue'],
                'city': CITY,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    if not records:
        log_message(
            'No dated event cards found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


class SpoletoUsaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='spoletousa_org',
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
        return scrape_concerts()


def main():
    SpoletoUsaOrgCrawler().run()


if __name__ == '__main__':
    main()
