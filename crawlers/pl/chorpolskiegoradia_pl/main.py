from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
import re

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://chorpolskiegoradia.pl/'
SOURCE = 'Chór Polskiego Radia – Lusławice'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/wp/v2/event'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.8',
}
COUNTRY_NAMES = {
    'austria': 'AT', 'belgia': 'BE', 'belgium': 'BE', 'czechy': 'CZ',
    'czech republic': 'CZ', 'france': 'FR', 'francja': 'FR', 'germany': 'DE',
    'hiszpania': 'ES', 'italia': 'IT', 'italy': 'IT', 'niemcy': 'DE',
    'niderlandy': 'NL', 'polska': 'PL', 'portugal': 'PT', 'portugalia': 'PT',
    'slovakia': 'SK', 'słowacja': 'SK', 'spain': 'ES', 'szwajcaria': 'CH',
    'szwecja': 'SE', 'sweden': 'SE', 'switzerland': 'CH',
    'united kingdom': 'GB', 'węgry': 'HU', 'wielka brytania': 'GB',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1.entry-title > span'))
    start = soup.select_one('time.dtstart[datetime]')
    event_date = clean_text(start.get('datetime')) if start else ''
    try:
        event_date = date.fromisoformat(event_date).isoformat()
    except ValueError:
        event_date = ''

    time_text = clean_text(soup.select_one('.fy-post-time'))
    match = re.search(r'(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)', time_text)
    time_from = f'{int(match.group(1)):02d}:{match.group(2)}' if match else None

    venue = clean_text(soup.select_one('.fy-post-location-title[itemprop="name"]'))
    city = clean_text(soup.select_one('.fy-post-location-city[itemprop="addressLocality"]'))
    city = re.sub(r'^\d{2}[ -]?\d{3}\s+', '', city).strip()
    street = clean_text(soup.select_one('.fy-post-location-street[itemprop="streetAddress"]'))
    country_code = 'PL'
    address_parts = {
        part.strip().casefold()
        for part in re.split(r'[,;\n]', f'{street}, {city}')
        if part.strip()
    }
    for country_name, code in COUNTRY_NAMES.items():
        if country_name in address_parts:
            country_code = code
            break
    description = clean_text(soup.select_one('.fy-post-content[itemprop="description"]')) or None

    if not all((title, event_date, url, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class ChorPolskiegoRadiaPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chorpolskiegoradia_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def _event_urls(self, session):
        urls = []
        page = 1
        while True:
            response = session.get(
                EVENTS_API_URL,
                params={'per_page': 100, 'page': page, '_fields': 'link'},
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
            urls.extend(clean_text(item.get('link')) for item in payload if item.get('link'))
            total_pages = int(response.headers.get('X-WP-TotalPages', page))
            if page >= total_pages:
                return urls
            page += 1

    def _fetch_event(self, session, url):
        response = session.get(url, headers=HEADERS, timeout=45)
        response.raise_for_status()
        return parse_event_page(response.text, url)

    def scrape(self):
        session = requests.Session()
        urls = self._event_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(self._fetch_event, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                    else:
                        log_message(
                            'Skipped incomplete Polish Radio Choir event',
                            event='crawler_item_skipped',
                            level='warning',
                            url=url,
                            error_type='IncompleteEventData',
                            error_message='Required title, date, venue, or city is missing',
                        )
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Polish Radio Choir event',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    ChorPolskiegoRadiaPlCrawler().run()


if __name__ == '__main__':
    main()
