import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.kokomosymphony.com/'
LISTING_URL = f'{SOURCE_URL}concerts-events/'
SOURCE = 'Kokomo Symphony Orchestra'
CITY = 'Kokomo'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Za-z]+\s+\d{1,2},\s+\d{4})\s*\|\s*'
    r'(\d{1,2}(?::\d{2})?\s*[AP]M)',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\r', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_time(value):
    match = DATE_TIME_RE.search(clean_text(value))
    if not match:
        return '', None
    try:
        event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
        time_from = datetime.strptime(match.group(2).upper(), '%I:%M %p').strftime('%H:%M')
    except ValueError:
        return '', None
    return event_date, time_from


def parse_card(card):
    front = card.select_one('.flipbox-front-description')
    back = card.select_one('.flipbox-back-layout')
    if not front:
        return None

    heading_lines = []
    for heading in front.find_all('h4'):
        lines = [clean_text(line) for line in heading.stripped_strings]
        if lines:
            heading_lines = lines
            break
    if not heading_lines:
        return None
    title = heading_lines[-1] if heading_lines else ''

    front_text = clean_text(front)
    event_date, time_from = parse_date_time(front_text)
    venue_match = re.search(r'\bVenue:\s*([^\n]+)', front_text, re.IGNORECASE)
    venue = clean_text(venue_match.group(1)) if venue_match else ''
    if not title or not event_date or not venue:
        return None

    description = None
    if back:
        description_node = BeautifulSoup(str(back), 'html.parser')
        for unwanted in description_node.select('form, table, input, select, button'):
            unwanted.decompose()
        description = clean_text(description_node) or None

    return {
        'title': title,
        'date': event_date,
        'url': LISTING_URL,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(LISTING_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    records = []
    for card in soup.select('.cfb-box-wrapper'):
        record = parse_card(card)
        if record:
            records.append(record)

    if not records:
        log_message(
            'No concert cards found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class KokomoSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kokomosymphony_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
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
    KokomoSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
