import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.southwoldartsfestival.co.uk/'
SOURCE = 'Southwold Arts Festival'
LISTING_URL = urljoin(SOURCE_URL, 'festival-events/')
CITY = 'Southwold'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def listing_urls(session):
    soup = get_soup(session, LISTING_URL)
    return {
        urljoin(LISTING_URL, link.get('href'))
        for link in soup.select('a.event_title[href]')
    }


def parse_start(value):
    if not value:
        return None
    match = re.search(
        r'(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})'
        r'(?:\s*,\s*(\d{1,2})[.:](\d{2})\s*(am|pm))?',
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    day, month, year, hour, minute, meridiem = match.groups()
    try:
        start = datetime.strptime(f'{day} {month} {year}', '%d %B %Y')
    except ValueError:
        return None
    if hour is None:
        return start, None
    parsed_hour = int(hour) % 12
    if meridiem.lower() == 'pm':
        parsed_hour += 12
    return start, f'{parsed_hour:02d}:{int(minute):02d}'


def detail_record(session, url):
    soup = get_soup(session, url)
    title = clean_text(soup.select_one('h3.heading-uppercase-heavy-size-8'))
    facts = soup.select_one('ul.list-reset-inline.text')
    fact_items = facts.select('li') if facts else []
    parsed_start = parse_start(clean_text(fact_items[0])) if fact_items else None
    venue = clean_text(fact_items[1]) if len(fact_items) > 1 else ''

    content = soup.select_one('.post-content')
    paragraphs = []
    if content:
        for paragraph in content.select('p'):
            text = clean_text(paragraph)
            if text and not re.search(r'\b(?:ticket|book)\b', text, re.IGNORECASE):
                paragraphs.append(text)
    description = '\n\n'.join(paragraphs) or None

    if not title or not parsed_start or not venue:
        return None
    start, time_from = parsed_start
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_events():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(detail_record, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Southwold Arts Festival event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class SouthwoldArtsFestivalCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='southwoldartsfestival_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_events()


def main():
    SouthwoldArtsFestivalCoUkCrawler().run()


if __name__ == '__main__':
    main()
