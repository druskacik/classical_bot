import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operagrandavignon.fr/'
AGENDA_URL = urljoin(SOURCE_URL, 'agenda')
SOURCE = 'Opéra Grand Avignon'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized(value):
    return ''.join(
        character for character in unicodedata.normalize('NFD', value.lower())
        if unicodedata.category(character) != 'Mn'
    )


def canonical_url(value):
    url = urljoin(SOURCE_URL, value or '')
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def make_session():
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    session.headers.update(HEADERS)
    return session


def detail_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = set()
    for card in soup.select('.card'):
        candidates = []
        for link in card.select('a[href]'):
            url = canonical_url(link.get('href'))
            parts = urlsplit(url)
            if parts.netloc == urlsplit(SOURCE_URL).netloc and parts.path not in ('/', '/agenda'):
                candidates.append(url)
        if candidates:
            urls.add(candidates[-1])
    return urls


def page_numbers(html):
    soup = BeautifulSoup(html, 'html.parser')
    pages = {0}
    for link in soup.select('.pager a[href], nav[aria-label="Pagination"] a[href]'):
        values = parse_qs(urlsplit(link.get('href')).query).get('page', [])
        if values and values[0].isdigit():
            pages.add(int(values[0]))
    return pages


def parse_session(value):
    text = normalized(clean_text(value))
    match = re.search(
        r'(\d{1,2})\s+(janvier|fevrier|mars|avril|mai|juin|juillet|aout|'
        r'septembre|octobre|novembre|decembre)\s+(20\d{2})'
        r'(?:\s+a\s+(\d{1,2})h(\d{2})?)?',
        text,
    )
    if not match:
        return None
    try:
        event_date = date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None
    time_from = None
    if match.group(4):
        time_from = f'{int(match.group(4)):02d}:{match.group(5) or "00"}'
    return event_date, time_from


def parse_location(soup):
    map_card = soup.select_one('.map-card')
    if not map_card:
        return None
    lines = [clean_text(item) for item in map_card.stripped_strings]
    lines = [item for item in lines if item and normalized(item) != 'voir sur la carte']
    if not lines:
        return None
    venue = lines[0]
    location_text = ' '.join(lines[1:])
    postal_city = re.search(r'\b\d{5}\s+([^,|]+)', location_text)
    if postal_city:
        city = clean_text(postal_city.group(1))
    else:
        city_match = re.search(r'\(([^()]+)\)\s*$', venue)
        city = clean_text(city_match.group(1)) if city_match else 'Avignon'
    venue = re.sub(r'\s*\([^()]+\)\s*$', '', venue).strip()
    if (
        not venue
        or not city
        or re.fullmatch(r'\d{5}\s+.+', venue)
        or normalized(venue) == normalized(city)
    ):
        return None
    return venue, city


def parse_detail(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('main h1.heading, main h1'))
    location = parse_location(soup)
    sessions = [parse_session(item) for item in soup.select('.fiche-seances li')]
    sessions = [item for item in sessions if item]
    if not title or not location or not sessions:
        return []

    description_parts = []
    for tab in soup.select('.tabs__content')[:2]:
        text = clean_text(tab)
        if text and text not in description_parts:
            description_parts.append(text)
    description = '\n\n'.join(description_parts) or None
    venue, city = location
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'FR',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from in sessions
    ]


class OperaGrandAvignonFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operagrandavignon_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = make_session()
        response = session.get(AGENDA_URL, timeout=45)
        response.raise_for_status()

        urls = detail_urls(response.text)
        for page in range(1, max(page_numbers(response.text)) + 1):
            page_response = session.get(AGENDA_URL, params={'page': page}, timeout=45)
            page_response.raise_for_status()
            urls.update(detail_urls(page_response.text))

        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(session.get, url, timeout=45): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    detail_response = future.result()
                    detail_response.raise_for_status()
                    parsed = parse_detail(url, detail_response.text)
                    if not parsed:
                        log_message(
                            'Skipped incomplete Opera Grand Avignon event',
                            event='crawler_item_skipped',
                            level='warning',
                            url=url,
                            error_type='IncompleteEventData',
                            error_message='Required session date, venue, city, or title is missing',
                        )
                    records.extend(parsed)
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Opera Grand Avignon event detail',
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


def main():
    OperaGrandAvignonFrCrawler().run()


if __name__ == '__main__':
    main()
