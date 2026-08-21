import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://domspatzen.de/'
SOURCE = 'Regensburger Domspatzen'
SITEMAP_URL = urljoin(SOURCE_URL, 'event-sitemap.xml')
CONCERT_CATEGORY_PATH = '/veranstaltungen/kategorien/chor-konzert/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_and_time(value):
    date_match = re.search(r'\b(\d{1,2}\.\d{1,2}\.20\d{2})\b', value)
    if not date_match:
        return None, None
    try:
        event_date = datetime.strptime(date_match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None, None
    time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\s*Uhr\b', value)
    event_time = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    return event_date, event_time


def parse_location(element, title):
    lines = [line.strip(' ,') for line in clean_text(element).splitlines() if line.strip(' ,')]
    if len(lines) < 2:
        return None

    venue = lines[0]
    location_text = ' '.join(lines)
    postal_match = re.search(r'\b(?:[A-Z]{1,2}-)?\d{4,5}\s+([^,]+)', lines[-1])
    city = postal_match.group(1).strip() if postal_match else ''
    if not city:
        return None

    country_hint = f'{title} {location_text}'.lower()
    if '(ch)' in country_hint or re.search(r'\b(?:ch|schweiz|switzerland)\b', country_hint):
        country_code = 'CH'
    elif '(it)' in country_hint or re.search(r'\b(?:it|italien|italy|italia)\b', country_hint):
        country_code = 'IT'
    elif '(at)' in country_hint or re.search(r'\b(?:at|österreich|austria)\b', country_hint):
        country_code = 'AT'
    else:
        country_code = 'DE'
    return venue, city, country_code


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    category_links = soup.select('a[href*="/veranstaltungen/kategorien/"]')
    if not any(CONCERT_CATEGORY_PATH in link.get('href', '') for link in category_links):
        return None

    title = clean_text(soup.select_one('h1.vc_custom_heading'))
    details = soup.select_one('.event-detail-info')
    location = parse_location(soup.select_one('.event-detail-info-location'), title)
    event_date, time_from = parse_date_and_time(
        clean_text(soup.select_one('.event-detail-info-date'))
    )
    if not title or details is None or not event_date or not location:
        return None

    description_element = soup.select_one('.event-detail-content')
    if description_element:
        for link in description_element.select('a'):
            if 'zurück zur übersicht' in clean_text(link).lower():
                link.decompose()
    description = clean_text(description_element) or None
    venue, city, country_code = location
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


class DomspatzenDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='domspatzen_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        dedupe_subset=['url', 'date'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(SITEMAP_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Domspatzen event sitemap',
                event='crawler_fetch_failed',
                level='error',
                url=SITEMAP_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        sitemap = BeautifulSoup(response.content, 'xml')
        event_urls = [
            loc.get_text(strip=True)
            for loc in sitemap.select('url > loc')
            if '/veranstaltungen/' in loc.get_text(strip=True)
            and loc.get_text(strip=True).rstrip('/') != urljoin(SOURCE_URL, 'veranstaltungen').rstrip('/')
        ]

        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(session.get, url, timeout=45): url for url in event_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    detail_response = future.result()
                    detail_response.raise_for_status()
                    record = parse_event(detail_response.text, url)
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Domspatzen event',
                        event='crawler_event_fetch_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    DomspatzenDeCrawler().run()


if __name__ == '__main__':
    main()
