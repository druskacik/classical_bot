import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.teatrolafenice.it/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendario/')
SOURCE = 'Teatro La Fenice'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

VENUE_LOCATIONS = {
    'Teatro La Fenice': ('Venezia', 'IT'),
    'Sale Apollinee': ('Venezia', 'IT'),
    'Teatro Malibran': ('Venezia', 'IT'),
    'Chiesa di Santa Maria del Carmelo (Carmini)': ('Venezia', 'IT'),
    'Piazza San Marco': ('Venezia', 'IT'),
    'Teatro Goldoni': ('Venezia', 'IT'),
    'Basilica di San Marco': ('Venezia', 'IT'),
    'Gran Teatre del Liceu': ('Barcelona', 'ES'),
    'Ljubljana - Cankarjev dom': ('Ljubljana', 'SI'),
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def detail_description(url):
    soup = get_soup(url)
    section = soup.select_one('section.sn_text_aside')
    return clean_text(section) or None


def parse_calendar(soup):
    records = []
    for row in soup.select('.sn_calendar_block_list_row'):
        date_id = row.get('data-list-id', '')
        try:
            month, day, year = (int(part) for part in date_id.split('-'))
            event_date = date(year, month, day).isoformat()
        except (TypeError, ValueError):
            continue

        for item in row.select('.sn_calendar_block_list_row_group_i'):
            title = clean_text(item.select_one('.title'))
            venue = clean_text(item.select_one('.place'))
            link = item.select_one('a[href*="/event/"]')
            location = VENUE_LOCATIONS.get(venue)
            if not title or not venue or link is None or location is None:
                continue

            time_text = clean_text(item.select_one('.time'))
            match = re.fullmatch(r'(\d{1,2}):(\d{2})', time_text)
            time_from = None
            if match and 0 <= int(match.group(1)) <= 23 and 0 <= int(match.group(2)) <= 59:
                time_from = f'{int(match.group(1)):02d}:{match.group(2)}'

            city, country_code = location
            records.append({
                'title': title,
                'date': event_date,
                'url': urljoin(SOURCE_URL, link.get('href')),
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class TeatroLaFeniceItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatrolafenice_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            records = parse_calendar(get_soup(CALENDAR_URL))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Teatro La Fenice calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        descriptions = {}
        urls = sorted({record['url'] for record in records})
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(detail_description, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    descriptions[url] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Teatro La Fenice event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        for record in records:
            record['description'] = descriptions.get(record['url'])
        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    TeatroLaFeniceItCrawler().run()


if __name__ == '__main__':
    main()
