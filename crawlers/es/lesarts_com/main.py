import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lesarts.com/'
PROGRAM_URL = f'{SOURCE_URL}es/programacion.html'
SOURCE = 'Les Arts València'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True) if '<' in raw else raw
    text = unescape(text)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response.text


def listing_urls(session):
    # "Todos los estados" includes the current season's active performances
    # and any finished performances that Les Arts continues to publish.
    soup = BeautifulSoup(
        get_page(session, PROGRAM_URL, params={'filtre_historic': '1'}),
        'html.parser',
    )
    urls = set()
    for link in soup.select('a[href*="/programacion/c/"]'):
        url = (link.get('href') or '').split('?', 1)[0]
        if re.search(r'/programacion/c/\d+-[^/]+\.html$', url):
            urls.add(url)
    return sorted(urls)


def json_objects(value):
    if isinstance(value, list):
        for item in value:
            yield from json_objects(item)
    elif isinstance(value, dict):
        if value.get('@type') == 'Event':
            yield value
        for item in value.get('@graph') or []:
            yield from json_objects(item)


def page_events(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        yield from json_objects(payload)


def page_description(soup, event):
    parts = [clean_text(event.get('description'))]
    # The JSON-LD synopsis omits structured programme blocks on some concert
    # pages. These editor fields contain the published composers and works.
    parts.extend(clean_text(element) for element in soup.select('.cf.editor'))
    unique = []
    for part in parts:
        if part and part not in unique:
            unique.append(part)
    return '\n\n'.join(unique) or None


def make_record(event, page_url, description):
    title = clean_text(event.get('name'))
    event_url = clean_text(event.get('url')) or page_url
    start = clean_text(event.get('startDate'))
    location = event.get('location') or {}
    address = location.get('address') or {}
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    country = clean_text(address.get('addressCountry')).upper()

    try:
        start_at = datetime.fromisoformat(start.replace('Z', '+00:00'))
        event_date = start_at.date().isoformat()
        time_from = start_at.strftime('%H:%M') if 'T' in start else None
    except (TypeError, ValueError):
        return None

    if not country and city in {'Valencia', 'València', 'Madrid'}:
        country = 'ES'
    if not all((title, event_url, venue, city, country)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': event_url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_detail(session, url):
    soup = BeautifulSoup(get_page(session, url), 'html.parser')
    events = list(page_events(soup))
    if not events:
        return []
    description = page_description(soup, events[0])
    return [
        record
        for event in events
        if (record := make_record(event, url, description)) is not None
    ]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(scrape_detail, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
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
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class LesArtsComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lesarts_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    LesArtsComCrawler().run()


if __name__ == '__main__':
    main()
