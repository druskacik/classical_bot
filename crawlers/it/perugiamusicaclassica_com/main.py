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


SOURCE_URL = 'https://perugiamusicaclassica.com/'
SOURCE = 'Fondazione Perugia Musica Classica'
EVENT_ARCHIVE_URL = urljoin(SOURCE_URL, 'it/events/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.6',
}

MONTHS = {
    'gennaio': 1,
    'febbraio': 2,
    'marzo': 3,
    'aprile': 4,
    'maggio': 5,
    'giugno': 6,
    'luglio': 7,
    'agosto': 8,
    'settembre': 9,
    'ottobre': 10,
    'novembre': 11,
    'dicembre': 12,
}


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_italian_date(text):
    match = re.search(r'\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})\b', text)
    if not match:
        return None
    month = MONTHS.get(match.group(2).lower())
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_location(text):
    parts = [part.strip(' ,') for part in text.split(',', 1)]
    if len(parts) != 2 or not all(parts):
        return None
    city, venue = parts
    return venue, city


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.event.type-event')
    event = soup.select_one('.em-event-single')
    if article is None or event is None:
        return None

    title = clean_text(article.select_one('h1.entry-title'))
    event_text = clean_text(event)
    event_date = parse_italian_date(event_text)
    location_link = event.select_one('a[href*="/locations/"]')
    location = parse_location(clean_text(location_link))
    if not title or not event_date or not location:
        return None

    time_match = re.search(r'\b([01]\d|2[0-3]):[0-5]\d\b', event_text)
    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_match.group(0) if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': event_text or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def discover_event_urls(session):
    """Walk the public event post archive, including its retained past events."""
    urls = []
    seen = set()
    page_number = 1

    while True:
        page_url = EVENT_ARCHIVE_URL if page_number == 1 else urljoin(
            EVENT_ARCHIVE_URL, f'page/{page_number}/'
        )
        response = session.get(page_url, timeout=45)
        if response.status_code == 404:
            break
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        articles = soup.select('main article.type-event')
        if not articles:
            break

        page_urls = []
        for article in articles:
            # The archive also contains talks and other festival activities.
            # Concerti is the site's first-party event category.
            classes = article.get('class') or []
            if 'event-categories-concerti' not in classes:
                continue
            link = article.select_one('h2.entry-title a[href], h1.entry-title a[href]')
            if link is not None:
                page_urls.append(urljoin(SOURCE_URL, link['href']))

        new_urls = [url for url in page_urls if url not in seen]
        urls.extend(new_urls)
        seen.update(new_urls)

        next_link = soup.select_one('a[href*="/events/page/%d/"]' % (page_number + 1))
        if next_link is None:
            break
        page_number += 1

    return urls


class PerugiaMusicaClassicaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='perugiamusicaclassica_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = make_session()
        try:
            urls = discover_event_urls(session)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Perugia Musica Classica event archive',
                event='crawler_fetch_failed',
                level='error',
                url=EVENT_ARCHIVE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(session.get, url, timeout=45): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    response = future.result()
                    response.raise_for_status()
                    record = parse_event(response.text, url)
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Perugia Musica Classica event',
                        event='crawler_item_failed',
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
    PerugiaMusicaClassicaComCrawler().run()


if __name__ == '__main__':
    main()
