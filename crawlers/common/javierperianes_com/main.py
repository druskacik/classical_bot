import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.javierperianes.com/'
SOURCE = 'Javier Perianes'
COLLECTION_URL = urljoin(SOURCE_URL, 'concerts-input?format=json')
SITE_TIMEZONE = ZoneInfo('Europe/Berlin')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-GB,en;q=0.9',
}

COUNTRY_CODES = {
    'Argentina': 'AR',
    'Australia': 'AU',
    'Austria': 'AT',
    'Belgium': 'BE',
    'Brazil': 'BR',
    'Canada': 'CA',
    'China': 'CN',
    'Costa Rica': 'CR',
    'Czech Republic': 'CZ',
    'England': 'GB',
    'Finland': 'FI',
    'France': 'FR',
    'Francia': 'FR',
    'Germany': 'DE',
    'Italy': 'IT',
    'Monaco': 'MC',
    'New Zealand': 'NZ',
    'Panamá': 'PA',
    'Poland': 'PL',
    'Portugal': 'PT',
    'Principality of Monaco': 'MC',
    'Romania': 'RO',
    'Scotland': 'GB',
    'Spain': 'ES',
    'UK': 'GB',
    'United Kingdom': 'GB',
    'United States': 'US',
    'Uruguay': 'UY',
    'USA': 'US',
}

PRESENTER_MARKERS = (
    'festival',
    'schubertíada',
    'piano aux jacobins',
)


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(value, 'html.parser')
    for link in soup.select('a'):
        if re.fullmatch(r'\s*more info\s*', link.get_text(' ', strip=True), re.IGNORECASE):
            link.decompose()
    text = soup.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_place(title):
    parts = [part.strip() for part in title.split(',') if part.strip()]
    if len(parts) < 2:
        return None
    country_code = COUNTRY_CODES.get(parts[-1])
    if country_code is None:
        return None
    city = parts[0]
    city = {'Lispon': 'Lisbon', 'Pölten': 'St. Pölten'}.get(city, city)
    return city, country_code


def parse_venue(excerpt):
    soup = BeautifulSoup(excerpt or '', 'html.parser')
    paragraphs = [
        element.get_text(' ', strip=True)
        for element in soup.select('p')
        if element.get_text(' ', strip=True)
        and not re.fullmatch(
            r'\s*more info\s*', element.get_text(' ', strip=True), re.IGNORECASE
        )
    ]
    if not paragraphs:
        return None
    if any(marker in paragraphs[0].lower() for marker in PRESENTER_MARKERS):
        return paragraphs[1] if len(paragraphs) > 1 else None
    return paragraphs[0]


def parse_event(item):
    title = (item.get('title') or '').strip()
    place = parse_place(title)
    venue = parse_venue(item.get('excerpt'))
    start_timestamp = item.get('startDate')
    path = item.get('fullUrl')
    if not title or not place or not venue or not start_timestamp or not path:
        return None

    try:
        start = datetime.fromtimestamp(start_timestamp / 1000, tz=SITE_TIMEZONE)
    except (OSError, OverflowError, TypeError, ValueError):
        return None

    city, country_code = place
    return {
        'title': f'Javier Perianes — {title}',
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, path),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_text(item.get('excerpt')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class JavierperianesComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='javierperianes_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
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
        dedupe_subset=['url'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        url = COLLECTION_URL
        seen_urls = set()
        records = []

        while url:
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Javier Perianes concert feed',
                    event='crawler_fetch_failed',
                    level='error',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            for item in payload.get('upcoming', []) + payload.get('past', []):
                record = parse_event(item)
                if record and record['url'] not in seen_urls:
                    seen_urls.add(record['url'])
                    records.append(record)

            next_path = payload.get('pagination', {}).get('nextPageUrl')
            if not next_path:
                break
            separator = '&' if '?' in next_path else '?'
            next_url = urljoin(SOURCE_URL, f'{next_path}{separator}format=json')
            if next_url == url:
                break
            url = next_url

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    JavierperianesComCrawler().run()


if __name__ == '__main__':
    main()
