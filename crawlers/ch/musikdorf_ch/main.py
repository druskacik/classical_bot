import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.musikdorf.ch/de'
PROGRAM_URL = f'{SOURCE_URL}/saisonprogramm'
SOURCE = 'Festival Musikdorf Ernen'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-CH,de;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1, 'februar': 2, 'märz': 3, 'april': 4, 'mai': 5,
    'juni': 6, 'juli': 7, 'august': 8, 'september': 9,
    'oktober': 10, 'november': 11, 'dezember': 12,
}
DATE_RE = re.compile(
    r'(?P<day>\d{1,2})\.\s*(?P<month>' + '|'.join(MONTHS) + r')\s+(?P<year>\d{4})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b([01]?\d|2[0-3])(?::([0-5]\d))?\s*Uhr\b', re.IGNORECASE)
CITY_SUFFIXES = ('Ernen', 'Brig')


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(value)
    if not match:
        return None
    try:
        return date(
            int(match.group('year')),
            MONTHS[match.group('month').casefold()],
            int(match.group('day')),
        ).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{int(match.group(2) or 0):02d}'


def parse_location(value):
    location = ' '.join(clean_text(value).split())
    for city in CITY_SUFFIXES:
        if not re.search(rf'\b{re.escape(city)}$', location, re.IGNORECASE):
            continue
        venue = re.sub(rf'(?:\s+{re.escape(city)})+$', '', location, flags=re.IGNORECASE).strip(' ,')
        if venue and venue.casefold() != city.casefold():
            return venue, city
    return None


def parse_listing(html):
    soup = BeautifulSoup(html, 'html.parser')
    events = []
    for block in soup.select('.event_date_list'):
        link = block.select_one('.event_title_wrap h3 a[href*="event_id-"]')
        title = clean_text(link)
        subtitle = clean_text(block.select_one('.event_title_wrap .subtitle'))
        if subtitle and subtitle.casefold() not in title.casefold():
            title = f'{title} – {subtitle}'
        event_date = parse_date(clean_text(block.select_one('.event_date')))
        location = parse_location(block.select_one('.event_location'))
        url = link.get('href', '').strip() if link else ''
        if not title or not event_date or not url or not location:
            continue
        venue, city = location
        events.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(clean_text(block.select_one('.event_time'))),
            'venue': venue,
            'city': city,
            'country_code': 'CH',
            'description': None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return events


def fetch_description(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    description = clean_text(soup.select_one('.event_detail_description'))
    return description or None


class MusikdorfChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musikdorf_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(PROGRAM_URL, timeout=45)
        response.raise_for_status()
        records = parse_listing(response.text)

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(fetch_description, session, record['url']): record
                for record in records
            }
            for future in as_completed(futures):
                record = futures[future]
                try:
                    record['description'] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Musikdorf Ernen event details',
                        event='crawler_item_failed',
                        level='warning',
                        url=record['url'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        log_message(
            'Musikdorf Ernen season scraped',
            event='crawler_scrape_completed',
            record_count=len(records),
        )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['url']))


def main():
    MusikdorfChCrawler().run()


if __name__ == '__main__':
    main()
