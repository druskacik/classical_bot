import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.angers-nantes-opera.com/'
SOURCE = 'Angers Nantes Opéra'
LISTING_URLS = [
    urljoin(SOURCE_URL, 'programmation'),
    urljoin(SOURCE_URL, 'archives_angers_nantes_opera?subsections=saison_25-26'),
    urljoin(SOURCE_URL, 'archives_angers_nantes_opera?subsections=saison_24-25'),
]
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.6',
}
DEFAULT_VENUES = {
    'Angers': 'Grand-Théâtre',
    'Nantes': 'Théâtre Graslin',
}
INVALID_PLACES = {
    ('Angers', 'en extérieur'),
    ('Entre Angers et Nantes', 'le long de la Loire'),
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def detail_urls(session):
    urls = set()
    for listing_url in LISTING_URLS:
        soup = get_soup(session, listing_url)
        for link in soup.select('.variation-contents__title a[href]'):
            url = urljoin(SOURCE_URL, link.get('href'))
            if url.startswith(SOURCE_URL):
                urls.add(url)
    return sorted(urls)


def event_objects(soup):
    events = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or '')
        except (json.JSONDecodeError, TypeError):
            continue
        values = value if isinstance(value, list) else [value]
        events.extend(
            item for item in values
            if isinstance(item, dict) and item.get('@type') == 'Event'
        )
    return events


def occurrence_places(soup):
    places = []
    for group in soup.select('.o-content_booking-item'):
        place_node = group.select_one('.o-content_timeline-title')
        place = clean_text(place_node.get_text(' ', strip=True)) if place_node else ''
        if not place:
            continue
        if ',' in place:
            city, venue = (part.strip() for part in place.split(',', 1))
        else:
            city = place.strip()
            venue = DEFAULT_VENUES.get(city, '')
        occurrence_count = len(group.select('a.o-booking'))
        if not city or not venue or (city, venue) in INVALID_PLACES:
            places.extend(None for _ in range(occurrence_count))
        else:
            places.extend((city, venue) for _ in range(occurrence_count))
    return places


def page_description(soup):
    body = soup.select_one('.o-content_body__left')
    if not body:
        body = soup.select_one('.o-content_body')
    return clean_text(body.get_text('\n', strip=True)) or None if body else None


def parse_detail(session, url):
    soup = get_soup(session, url)
    events = event_objects(soup)
    places = occurrence_places(soup)
    description = page_description(soup)
    records = []
    for index, event in enumerate(events):
        start = event.get('startDate')
        try:
            start_at = datetime.fromisoformat(start)
        except (TypeError, ValueError):
            continue
        location = event.get('location') or {}
        address = location.get('address') or {}
        city = clean_text(address.get('addressLocality'))
        venue = clean_text(location.get('name'))
        if (not city or not venue) and index < len(places) and places[index]:
            city, venue = places[index]
        title = clean_text(event.get('name'))
        if not all((title, city, venue)):
            continue
        records.append({
            'title': title,
            'date': start_at.date().isoformat(),
            'url': url,
            'time_from': start_at.strftime('%H:%M'),
            'venue': venue,
            'city': city,
            'country_code': 'FR',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    urls = detail_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(parse_detail, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['city'], item['url']
        ),
    )


class AngersNantesOperaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='angers_nantes_opera_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    AngersNantesOperaComCrawler().run()


if __name__ == '__main__':
    main()
