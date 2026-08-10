import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from math import ceil
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.neue-philharmonie-westfalen.de/'
EVENTS_API = urljoin(SOURCE_URL, 'ajax/events')
SOURCE = 'Neue Philharmonie Westfalen'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def parse_listing(html):
    soup = BeautifulSoup(html, 'html.parser')
    events = []
    for item in soup.select('.c-event'):
        time_tag = item.select_one('time[datetime]')
        link = item.select_one('a.c-event__link[href]')
        title = clean_text(item.select_one('.c-event__title'))
        venue = clean_text(item.select_one('.c-event__venue'))
        city = clean_text(item.select_one('.c-event__city'))
        if not time_tag or not link or not title or not venue or not city:
            continue
        try:
            start = datetime.strptime(time_tag['datetime'].strip(), '%Y-%m-%d %H:%M:%S')
        except (KeyError, ValueError):
            continue
        events.append({
            'title': title,
            'date': start.date().isoformat(),
            'url': urljoin(SOURCE_URL, link['href']),
            'time_from': start.strftime('%H:%M'),
            'venue': venue,
            'city': city,
        })
    return events


def listing_events(session):
    first = get_response(session, EVENTS_API, params={'page': 1}).json()
    events = parse_listing(first.get('events') or '')
    total = int(first.get('total') or len(events))
    limit = int(first.get('limit') or 10)
    for page in range(2, ceil(total / limit) + 1):
        payload = get_response(session, EVENTS_API, params={'page': page}).json()
        events.extend(parse_listing(payload.get('events') or ''))
    return events


def detail_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    parts = []

    programme = soup.select_one('#programm-tab .m-text__text')
    programme_text = clean_text(programme)
    if programme_text:
        parts.append('Programm\n' + programme_text)

    for module in soup.select('main > .content-main > .module.m-text'):
        heading = clean_text(module.select_one('.c-heading__headline'))
        body = clean_text(module.select_one('.m-text__text'))
        if body and body not in parts:
            parts.append(f'{heading}\n{body}' if heading else body)

    return clean_text('\n\n'.join(parts)) or None


def enrich_event(event):
    try:
        response = requests.get(event['url'], headers=HEADERS, timeout=45)
        response.raise_for_status()
        description = detail_description(response.text)
    except requests.RequestException as error:
        log_message(
            'Failed to scrape concert detail',
            event='crawler_item_failed',
            level='warning',
            url=event['url'],
            error_type=type(error).__name__,
            error_message=str(error),
        )
        description = None
    return {
        **event,
        'country_code': 'DE',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = listing_events(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(enrich_event, event) for event in events]
        for future in as_completed(futures):
            records.append(future.result())
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class NeuePhilharmonieWestfalenDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='neue_philharmonie_westfalen_de',
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
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    NeuePhilharmonieWestfalenDeCrawler().run()


if __name__ == '__main__':
    main()
