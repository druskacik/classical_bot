import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.opera-lyon.com/'
API_URL = 'https://api.opera-lyon.com/api/events'
SOURCE = 'Opéra national de Lyon'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
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
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response


def listing_events(session):
    # The public programme API includes the site's published archive when no
    # date or season constraint is supplied. Restrict only to concrete live
    # performance candidates; artistic scope remains deliberately unfiltered.
    params = [
        ('itemsPerPage', '100'),
        ('_locale', 'fr'),
        ('exists[superEvent]', 'false'),
        ('type[]', 'live_performance'),
        ('properties[]', 'name'),
        ('properties[]', 'url'),
        ('properties[]', 'datesCount'),
        ('properties[]', 'placesNames'),
        ('order[sortingDateTime]', 'ASC'),
    ]
    url = API_URL
    events = []
    while url:
        payload = get_response(session, url, params=params).json()
        events.extend(payload.get('hydra:member') or [])
        view = payload.get('hydra:view') or {}
        next_path = view.get('hydra:next')
        url = urljoin(API_URL, next_path) if next_path else None
        params = None
    return [event for event in events if event.get('datesCount', 0) > 0]


def description_from_page(soup):
    parts = []
    presentation = soup.select_one('section#presentation')
    if presentation:
        value = clean_text(presentation.get_text('\n', strip=True))
        if value:
            parts.append(value)

    # The structured occurrence description is generally the editorial
    # synopsis and is useful when the presentation block is absent.
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or '')
        except (TypeError, ValueError):
            continue
        values = payload if isinstance(payload, list) else [payload]
        for value in values:
            if isinstance(value, dict) and value.get('@type') in (
                'Event', 'http://schema.org/Event', 'https://schema.org/Event'
            ):
                synopsis = clean_text(value.get('description'))
                if synopsis and synopsis not in parts:
                    parts.append(synopsis)
    return '\n\n'.join(parts) or None


def event_records(session, event):
    relative_url = event.get('url')
    if not relative_url:
        return []
    url = urljoin(SOURCE_URL, relative_url)
    soup = BeautifulSoup(get_response(session, url).text, 'html.parser')
    description = description_from_page(soup)
    records = []

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or '')
        except (TypeError, ValueError):
            continue
        values = payload if isinstance(payload, list) else [payload]
        for occurrence in values:
            if not isinstance(occurrence, dict) or occurrence.get('@type') not in (
                'Event', 'http://schema.org/Event', 'https://schema.org/Event'
            ):
                continue
            location = occurrence.get('location') or {}
            address = location.get('address') or {}
            title = clean_text(occurrence.get('name') or event.get('name'))
            venue = clean_text(location.get('name'))
            city = clean_text(address.get('addressLocality'))
            places = event.get('placesNames') or {}
            if len(places) == 1:
                # Some off-site events retain the Opera's address in their
                # JSON-LD, while the API's first-party place is correct.
                venue = clean_text(next(iter(places.values()))) or venue
                lyon_match = re.search(r',\s*Lyon(?:\s+\d+(?:e|er)?)?$', venue, re.I)
                if lyon_match:
                    venue = venue[:lyon_match.start()].strip()
                    city = 'Lyon'
            country = clean_text(address.get('addressCountry')).lower()
            start = occurrence.get('startDate') or ''
            match = re.match(r'(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})', start)
            if not all((title, venue, city, match)):
                continue
            if country not in ('fr', 'fra', 'france'):
                continue
            try:
                event_date = date.fromisoformat(match.group(1)).isoformat()
            except ValueError:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': f'{match.group(2)}:{match.group(3)}',
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
    events = listing_events(session)
    records = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {
            executor.submit(event_records, session, event): event for event in events
        }
        for future in as_completed(futures):
            event = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=urljoin(SOURCE_URL, event.get('url') or ''),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class OperaLyonComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_lyon_com',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OperaLyonComCrawler().run()


if __name__ == '__main__':
    main()
