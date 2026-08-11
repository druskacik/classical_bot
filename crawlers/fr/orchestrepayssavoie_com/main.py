import re
import unicodedata
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orchestrepayssavoie.com/'
SOURCE = 'Orchestre des Pays de Savoie'
SITEMAP_URL = f'{SOURCE_URL}wp-sitemap-posts-concerts-1.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def folded(value):
    value = unicodedata.normalize('NFKD', clean_text(value).casefold())
    return ''.join(char for char in value if not unicodedata.combining(char))


def parse_datetime(value):
    match = re.search(
        r'\b(\d{1,2})\s+([a-z]+)\s+(20\d{2})(?:\s+a\s+(\d{1,2})h([0-5]\d)?)?',
        folded(value),
    )
    if not match or match.group(2) not in MONTHS:
        return None, None
    try:
        event_date = datetime(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).date()
    except ValueError:
        return None, None
    time_from = None
    if match.group(4):
        time_from = f'{int(match.group(4)):02d}:{int(match.group(5) or 0):02d}'
    return event_date.isoformat(), time_from


def parse_location(value):
    text = clean_text(value)
    marker = re.search(r'\s*\(([A-Z]{2}|\d{2,3})\)\s*$', text)
    country_code = marker.group(1) if marker and marker.group(1).isalpha() else 'FR'
    city = text[:marker.start()].strip(' ,-') if marker else text
    return city, country_code


def parse_concert_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1'))
    content = soup.select_one('.content')
    description = clean_text(content) or None
    records = []

    for occurrence in soup.select('.dates .date'):
        paragraphs = occurrence.find_all('p', recursive=False)
        if len(paragraphs) < 3:
            continue
        event_date, time_from = parse_datetime(paragraphs[0])
        venue = clean_text(paragraphs[1])
        city, country_code = parse_location(paragraphs[2])
        if not title or not event_date or not venue or not city or folded(venue) == folded(city):
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
        })
    return records


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )))
    return session


class OrchestrePaysSavoieCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestrepayssavoie_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = make_session()
        response = session.get(SITEMAP_URL, timeout=45)
        response.raise_for_status()
        sitemap = BeautifulSoup(response.content, 'xml')
        urls = [clean_text(node) for node in sitemap.find_all('loc')]

        records = []
        for url in urls:
            try:
                page = session.get(url, timeout=45)
                page.raise_for_status()
                records.extend(parse_concert_page(page.text, url))
            except requests.RequestException as error:
                log_message(
                    'Concert page could not be fetched',
                    level='warning',
                    event='crawler_page_fetch_failed',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        log_message(
            'Concert pages parsed',
            level='info',
            event='crawler_pages_parsed',
            record_count=len(records),
        )
        return records


def main():
    return OrchestrePaysSavoieCrawler().run()


if __name__ == '__main__':
    main()
