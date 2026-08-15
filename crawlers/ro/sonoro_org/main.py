import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sonoro.org/'
SOURCE = 'Asociația SoNoRo'
PROJECT_HOSTS = (
    'festival.sonoro.org',
    'conac.sonoro.org',
    'musikland.sonoro.org',
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ro-RO,ro;q=0.9,en;q=0.6',
}
MONTHS = {
    'ianuarie': 1,
    'februarie': 2,
    'martie': 3,
    'aprilie': 4,
    'mai': 5,
    'iunie': 6,
    'iulie': 7,
    'august': 8,
    'septembrie': 9,
    'octombrie': 10,
    'noiembrie': 11,
    'decembrie': 12,
}
KNOWN_CITIES = (
    'București', 'Cluj-Napoca', 'Timișoara', 'Brașov', 'Sibiu', 'Sighișoara',
    'Arad', 'Ploiești', 'Iași', 'Oradea', 'Craiova', 'Constanța', 'Târgu Mureș',
    'Alba Iulia', 'Bistrița', 'Galați', 'Râșnov', 'Avrig', 'Viscri', 'Criț',
    'Cincu', 'Meșendorf', 'Cristian', 'Porumbacu de Sus', 'Măderat', 'Ghidigeni',
)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fold(value):
    return ''.join(
        character for character in unicodedata.normalize('NFKD', value.lower())
        if not unicodedata.combining(character)
    )


def parse_date_time(value):
    normalized = fold(value)
    match = re.search(
        r'\b(\d{1,2})\s+([a-z]+)\s*,?\s*(20\d{2})\b', normalized
    )
    if not match:
        return None, None
    month = MONTHS.get(match.group(2))
    if month is None:
        return None, None
    try:
        event_date = date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None, None

    time_match = re.search(r'\b(?:ora\s+)?([01]?\d|2[0-3])[:.]([0-5]\d)\b', normalized)
    event_time = None
    if time_match:
        event_time = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
    return event_date, event_time


def extract_city(address):
    normalized = fold(address)
    for city in sorted(KNOWN_CITIES, key=len, reverse=True):
        if re.search(rf'\b{re.escape(fold(city))}\b', normalized):
            return city

    county_match = re.search(r'(?:^|,)\s*(?:sat(?:ul)?\s+)?([^,]+?)\s*,?\s+judetul\b', normalized)
    if county_match:
        candidate = county_match.group(1).strip(' .-')
        if candidate and not re.search(r'\d|strada|piata|calea', candidate):
            return candidate.title()

    parts = [part.strip() for part in address.split(',') if part.strip()]
    if parts:
        candidate = re.sub(r'^\d{5,6}\s*', '', parts[-1]).strip(' .-')
        if re.fullmatch(r'[A-Za-zĂÂÎȘȚăâîșț -]{2,40}', candidate):
            return candidate
    return None


def parse_concert(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    info = soup.select_one('.concert-info')
    if info is None:
        return None

    title = clean_text(soup.select_one('main h2, .main-content h2, h2'))
    date_text = clean_text(info.select_one('h3'))
    event_date, event_time = parse_date_time(date_text)
    venue_element = info.select_one('a.h4, .h4')
    venue = clean_text(venue_element)
    address = clean_text(info.select_one('.col-md-6 p'))
    city = extract_city(address)
    if not title or not event_date or not venue or not city:
        return None

    description_parts = []
    for selector in ('.concert-descriere', '.concert-program'):
        value = clean_text(soup.select_one(selector))
        if value and value not in description_parts:
            description_parts.append(value)

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': 'RO',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class SonoroOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sonoro_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='RO',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = []
        for host in PROJECT_HOSTS:
            sitemap_url = f'https://{host}/concerte-sitemap.xml'
            try:
                response = session.get(sitemap_url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch SoNoRo concert sitemap',
                    event='crawler_fetch_failed',
                    level='error',
                    url=sitemap_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise
            sitemap = BeautifulSoup(response.text, 'xml')
            urls.extend(
                location.get_text(strip=True)
                for location in sitemap.select('url > loc')
                if '/concerte/' in location.get_text()
                and '/en/' not in location.get_text()
                and '/wp-content/' not in location.get_text()
            )

        records = []

        def fetch(url):
            response = session.get(url, timeout=45)
            response.raise_for_status()
            return parse_concert(url, response.text)

        with ThreadPoolExecutor(max_workers=8) as executor:
            future_urls = {executor.submit(fetch, url): url for url in sorted(set(urls))}
            for future in as_completed(future_urls):
                url = future_urls[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch SoNoRo concert',
                        event='crawler_fetch_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    SonoroOrgCrawler().run()


if __name__ == '__main__':
    main()
