import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.concertgebouw.nl/'
AGENDA_URL = urljoin(SOURCE_URL, 'concerten-en-tickets')
DETAIL_API_URL = urljoin(SOURCE_URL, 'api/default/nl/page.json')
SOURCE = 'Het Concertgebouw'
DEFAULT_CITY = 'Amsterdam'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}


def clean_html(value):
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


def event_links(session):
    links = set()
    page = 1
    last_page = 1
    while page <= last_page:
        response = get_response(session, AGENDA_URL, params={'page': page})
        soup = BeautifulSoup(response.text, 'html.parser')

        page_numbers = []
        for button in soup.select('nav button'):
            label = clean_html(button)
            if label.isdigit():
                page_numbers.append(int(label))
        last_page = max(page_numbers, default=last_page)

        for anchor in soup.select('a[href*="/concerten/"]'):
            url = urljoin(SOURCE_URL, anchor.get('href'))
            if re.fullmatch(r'/concerten/[^/]+', urlparse(url).path.rstrip('/')):
                links.add(url)
        page += 1
    return sorted(links)


def description_from_payload(payload):
    production = payload.get('production') or {}
    parts = []
    for heading, field in (
        (None, 'introduction'),
        ('Programma', 'program'),
        (None, 'description'),
    ):
        text = clean_html(production.get(field))
        if text and text not in parts:
            parts.append(f'{heading}\n{text}' if heading else text)

    works = []
    for work in production.get('works') or []:
        if not isinstance(work, dict):
            continue
        composer = clean_html((work.get('composer') or {}).get('title'))
        title = clean_html(work.get('title') or work.get('workTitle'))
        line = ' — '.join(value for value in (composer, title) if value)
        if line and line not in works:
            works.append(line)
    if works:
        parts.append('Programma\n' + '\n'.join(works))
    return '\n\n'.join(parts) or None


def make_record(payload, fallback_url):
    title = clean_html(payload.get('title'))
    event_url = payload.get('url') or fallback_url
    room = clean_html((payload.get('room') or {}).get('title'))
    start = payload.get('eventDate')
    try:
        parsed = datetime.fromisoformat(start.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return None
    if not title or not event_url or not room:
        return None
    return {
        'title': title,
        'date': parsed.date().isoformat(),
        'url': event_url,
        'time_from': parsed.strftime('%H:%M'),
        'venue': f'Het Concertgebouw – {room}',
        'city': DEFAULT_CITY,
        'country_code': 'NL',
        'description': description_from_payload(payload),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_detail(session, url):
    api_path = urlparse(url).path.lstrip('/')
    payload = get_response(session, DETAIL_API_URL, params={'url': api_path}).json()
    return make_record(payload, url)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    links = event_links(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(scrape_detail, session, url): url for url in links}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
                if record:
                    records.append(record)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Concertgebouw detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'], record['title'], record['url']),
    )


class ConcertgebouwNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='concertgebouw_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    ConcertgebouwNlCrawler().run()


if __name__ == '__main__':
    main()
