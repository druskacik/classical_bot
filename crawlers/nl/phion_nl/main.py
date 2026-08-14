import re
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.phion.nl/'
AGENDA_API = f'{SOURCE_URL}wp-json/lumbermill/v1/agenda/get_sessions'
SOURCE = 'Orkest Phion'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}

MONTHS = {
    'jan': 1, 'feb': 2, 'mrt': 3, 'apr': 4, 'mei': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'okt': 10, 'nov': 11, 'dec': 12,
}


def clean_text(value, separator=' '):
    if not value:
        return ''
    text = value.get_text(separator, strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def agenda_sessions(session):
    items = []
    page = 1
    while True:
        payload = get_json(
            session,
            AGENDA_API,
            params={'page': page, 'city': '', 'composer': '', 'conductor': '', 'soloist': ''},
        )
        page_items = payload.get('items') or []
        items.extend(item for item in page_items if isinstance(item, dict))
        if not payload.get('hasNextPage'):
            break
        page += 1
    return items


def parse_dutch_date(value):
    match = re.fullmatch(r'(\d{1,2})\s+([a-z]{3})\s+(\d{4})', clean_text(value).lower())
    if not match or match.group(2) not in MONTHS:
        return None
    try:
        return datetime(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).date().isoformat()
    except ValueError:
        return None


def description_from_page(soup):
    parts = []
    for selector in ('.flex-wysiwyg', '.flex-programme-information'):
        for node in soup.select(selector):
            text = clean_text(node, separator='\n')
            if text and text not in parts:
                parts.append(text)
    return '\n\n'.join(parts) or None


def detail_occurrences(session, programme_url):
    response = session.get(programme_url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    description = description_from_page(soup)
    occurrences = []
    for position, node in enumerate(soup.select('.single-programme-sidebar-item')):
        location = clean_text(node.select_one('.single-programme-sidebar-item__title'))
        if '—' not in location:
            continue
        city, venue = (clean_text(part) for part in location.split('—', 1))
        event_date = parse_dutch_date(node.select_one('.single-programme-sidebar-item__date'))
        time_text = clean_text(node.select_one('.single-programme-sidebar-item__time'))
        time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)', time_text)
        if not city or not venue or not event_date:
            continue
        occurrences.append({
            'date': event_date,
            'time_from': f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None,
            'venue': venue,
            'city': city,
            'description': description,
            '_position': position,
        })
    return occurrences


def session_url(programme_url, session_id):
    parts = urlsplit(programme_url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode({'session': session_id}), ''))


def scrape_programme(session, programme, api_items):
    programme_url = programme.get('url')
    title = clean_text(programme.get('title'))
    if not programme_url or not title:
        return []

    occurrences = detail_occurrences(session, programme_url)
    sessions_by_date = defaultdict(deque)
    for item in sorted(api_items, key=lambda value: (value.get('date', ''), value.get('id', 0))):
        try:
            event_date = datetime.strptime(item.get('date', ''), '%d-%m-%Y').date().isoformat()
        except (TypeError, ValueError):
            continue
        sessions_by_date[event_date].append(item)

    records = []
    for occurrence in sorted(occurrences, key=lambda value: (value['date'], value['_position'])):
        candidates = sessions_by_date[occurrence['date']]
        if not candidates:
            continue
        api_item = candidates.popleft()
        records.append({
            'title': title,
            'date': occurrence['date'],
            'url': session_url(programme_url, api_item['id']),
            'time_from': occurrence['time_from'],
            'venue': occurrence['venue'],
            'city': occurrence['city'],
            'country_code': 'NL',
            'description': occurrence['description'],
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = agenda_sessions(session)
    grouped = defaultdict(list)
    programmes = {}
    for item in items:
        programme = item.get('programme') or {}
        programme_id = programme.get('id')
        if programme_id and programme.get('url'):
            grouped[programme_id].append(item)
            programmes[programme_id] = programme

    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(scrape_programme, session, programmes[key], grouped[key]): programmes[key]['url']
            for key in programmes
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Phion programme',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title'], record['url']),
    )


class PhionNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='phion_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    PhionNlCrawler().run()


if __name__ == '__main__':
    main()
