from concurrent.futures import ThreadPoolExecutor
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.saocarlos.pt/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar/')
SOURCE = 'Teatro Nacional de São Carlos'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-PT,pt;q=0.9,en;q=0.7',
}

MONTHS = {
    'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
    'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12,
}


def build_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def text(element):
    if element is None:
        return ''
    return ' '.join(element.get_text(' ', strip=True).split())


def named(element, name):
    return text(element.select_one(f'[data-bl-name="{name}"]'))


def parse_date(card):
    try:
        return date(
            int(named(card, 'Year')),
            MONTHS[named(card, 'Month').casefold()[:3]],
            int(named(card, 'Day')),
        ).isoformat()
    except (KeyError, TypeError, ValueError):
        return None


def country_for_city(city):
    normalized = city.casefold()
    if 'estónia' in normalized or 'estonia' in normalized or 'saaremaa' in normalized:
        return 'EE'
    if 'brasil' in normalized or 'rio de janeiro' in normalized:
        return 'BR'
    if 'milão' in normalized or 'milano' in normalized:
        return 'IT'
    if 'nova iorque' in normalized or 'new york' in normalized:
        return 'US'
    return 'PT'


def parse_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    parts = []
    for selector in (
        '[data-bl-name="Sinopse_"] [data-bl-name="Description"]',
        '[data-bl-name="Setlist_"] [data-bl-name="Description"]',
    ):
        value = text(soup.select_one(selector))
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


class SaocarlosPtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='saocarlos_pt',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = build_session()
        try:
            response = session.get(CALENDAR_URL, timeout=60)
            response.raise_for_status()
            response.encoding = 'utf-8'
        except requests.RequestException as error:
            log_message(
                'Failed to fetch São Carlos calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        parsed = []
        for card in soup.select('a[data-bl-name="Card.Calendar"][href]'):
            title = named(card, 'Display Title')
            occurrence_label = named(card, 'Info tag')
            if occurrence_label and occurrence_label.casefold() != title.casefold():
                title = f'{title} — {occurrence_label}'
            event_date = parse_date(card)
            venue = named(card, 'Local')
            city = named(card, 'City')
            url = urljoin(SOURCE_URL, card['href'])
            if not all((title, event_date, venue, city, url)):
                log_message(
                    'Skipping São Carlos calendar entry with missing required fields',
                    event='crawler_record_skipped',
                    level='warning',
                    url=url,
                    has_title=bool(title),
                    has_date=bool(event_date),
                    has_venue=bool(venue),
                    has_city=bool(city),
                )
                continue
            parsed.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': named(card, 'Time') or None,
                'venue': venue,
                'city': city,
                'country_code': country_for_city(city),
            })

        if not parsed:
            raise ValueError('No valid occurrences found on the São Carlos calendar')

        urls = list(dict.fromkeys(record['url'] for record in parsed))

        def fetch_description(url):
            try:
                detail = session.get(url, timeout=45)
                detail.raise_for_status()
                detail.encoding = 'utf-8'
                return url, parse_description(detail.text)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch São Carlos event detail',
                    event='crawler_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                return url, None

        with ThreadPoolExecutor(max_workers=6) as executor:
            descriptions = dict(executor.map(fetch_description, urls))

        records = []
        for record in parsed:
            records.append({
                **record,
                'description': descriptions.get(record['url']),
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    SaocarlosPtCrawler().run()


if __name__ == '__main__':
    main()
