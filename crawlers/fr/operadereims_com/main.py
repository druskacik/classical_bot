import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://operadereims.com/'
SOURCE = 'Opéra de Reims'
LIST_URL = urljoin(SOURCE_URL, 'event/')
SITEMAP_URL = urljoin(SOURCE_URL, 'wp-sitemap-posts-event-1.xml')
DEFAULT_VENUE = 'Opéra de Reims'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janvier': 1, 'février': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'août': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'décembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


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


def listing_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    if response.ok:
        urls = re.findall(r'<loc>(.*?)</loc>', response.text)
        urls = [url for url in urls if '/event/' in url]
        if urls:
            return list(dict.fromkeys(urls))

    # Fall back to the visible season archive if WordPress disables its sitemap.
    urls = []
    page_number = 1
    while True:
        page_url = LIST_URL if page_number == 1 else urljoin(LIST_URL, f'page/{page_number}/')
        response = session.get(page_url, timeout=45)
        if response.status_code == 404:
            break
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        page_urls = [
            urljoin(SOURCE_URL, card.get('href'))
            for card in soup.select('a.component--card-event[href]')
        ]
        page_urls = list(dict.fromkeys(page_urls))
        if not page_urls:
            break
        new_urls = [url for url in page_urls if url not in urls]
        if not new_urls:
            break
        urls.extend(new_urls)
        page_number += 1
    return urls


def parse_occurrences(text):
    occurrences = []
    pattern = re.compile(
        r'\b(?:lun|mar|mer|jeu|ven|sam|dim)\.\s*'
        r'(\d{1,2})\s+([a-zéû]+)\s+(20\d{2})'
        r'(?:\s*-\s*(\d{1,2})h(\d{2}))?',
        re.IGNORECASE,
    )
    for match in pattern.finditer(text.casefold()):
        month = MONTHS.get(match.group(2))
        if not month:
            continue
        try:
            event_date = date(int(match.group(3)), month, int(match.group(1)))
        except ValueError:
            continue
        time_from = None
        if match.group(4):
            time_from = f'{int(match.group(4)):02d}:{int(match.group(5)):02d}'
        occurrence = (event_date.isoformat(), time_from)
        if occurrence not in occurrences:
            occurrences.append(occurrence)
    return occurrences


def resolve_place(title, text):
    combined = f'{title} {text[:1200]}'.casefold()
    if 'cormontreuil' in combined:
        # The site identifies the town but not the host venue.
        return None, None
    if 'au manège' in combined or 'manège, scène nationale' in combined:
        return 'Le Manège, scène nationale', 'Reims'
    return DEFAULT_VENUE, 'Reims'


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    root = soup.select_one('.post-type-event')
    title_node = root.select_one('h1') if root else None
    title = clean_text(title_node)
    if not root or not title:
        return []

    root_copy = BeautifulSoup(str(root), 'html.parser')
    for node in root_copy.select(
        '.component--card-event, .component--newsletter, script, style, '
        '.component__share, [class*="ticket"]'
    ):
        node.decompose()
    detail_text = clean_text(root_copy)
    occurrences = parse_occurrences(detail_text)
    venue, city = resolve_place(title, detail_text)
    if not occurrences or not venue or not city:
        return []

    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'description': detail_text or None,
    } for event_date, time_from in occurrences]


class OperaDeReimsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operadereims_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        dedupe_subset=['url', 'date', 'time_from'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        session = make_session()
        records = []
        urls = listing_urls(session)
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(session.get, url, timeout=45): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    response = future.result()
                    response.raise_for_status()
                    records.extend(parse_event(response.text, url))
                except requests.RequestException as error:
                    log_message(
                        'Event page request failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        records.sort(key=lambda record: (record['date'], record['time_from'] or '', record['title']))
        log_message(
            'Scraped Opéra de Reims events',
            level='info',
            record_count=len(records),
        )
        return records


def main():
    return OperaDeReimsCrawler().run()


if __name__ == '__main__':
    main()
