import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://stadttheater-giessen.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'de/kalender/')
AJAX_URL = urljoin(SOURCE_URL, 'de/ajax/')
SOURCE = 'Stadttheater Gießen'
CITY = 'Gießen'
DEFAULT_VENUE = 'Stadttheater Gießen'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

VENUE_ALIASES = {
    'großes haus': 'Stadttheater Gießen – Großes Haus',
    'grosses haus': 'Stadttheater Gießen – Großes Haus',
    'kleines haus': 'Stadttheater Gießen – Kleines Haus',
    'foyer gh': 'Stadttheater Gießen – Foyer Großes Haus',
    'foyer kh': 'Stadttheater Gießen – Foyer Kleines Haus',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text(
        '\n', strip=True
    )
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def published_months(session):
    soup = get_soup(session, CALENDAR_URL)
    months = set()
    for node in soup.select('input[name="eventsFilterMonth"][data-year][value]'):
        try:
            year = int(node['data-year'])
            month = int(node['value'])
            date(year, month, 1)
        except (KeyError, TypeError, ValueError):
            continue
        months.add((year, month))
    return sorted(months)


def month_items(session, year, month):
    response = session.get(
        AJAX_URL,
        params={
            'action': 'load_items',
            'selectedYoungAudience': 'false',
            'selectedMonth': month,
            'selectedYear': year,
        },
        headers={'X-Requested-With': 'XMLHttpRequest'},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get('success') or not isinstance(payload.get('data'), list):
        raise ValueError(f'Unexpected calendar response for {year}-{month:02d}')
    return payload['data']


def resolve_venue(item):
    location = clean_text(item.get('location'))
    details = clean_text(item.get('locationDetails'))
    more_info = clean_text(item.get('dateMoreInfo'))

    for value in (details, location, more_info):
        normalized = value.casefold()
        for alias, venue in VENUE_ALIASES.items():
            if alias in normalized:
                return venue

    if details:
        # A street address is useful for navigation but is not a venue name.
        if re.search(r'\b\d+[a-z]?\b', details, flags=re.IGNORECASE):
            return None
        return details
    if location and location.casefold() != 'diverse spielorte':
        return location

    # These entries are explicitly mobile or at varying venues. Without a
    # venue in the occurrence data, assigning the theatre building is unsafe.
    if location.casefold() == 'diverse spielorte':
        return None
    return DEFAULT_VENUE


def item_record(item, year, month):
    title = clean_text(item.get('title'))
    relative_url = item.get('url') or ''
    date_match = re.fullmatch(r'(\d{1,2})\.(\d{1,2})\.', str(item.get('date', '')))
    venue = resolve_venue(item)
    if not title or not relative_url or not date_match or not venue:
        return None

    day, item_month = map(int, date_match.groups())
    if item_month != month:
        return None
    try:
        event_date = date(year, month, day).isoformat()
    except ValueError:
        return None

    start_time = clean_text(item.get('startTime')) or None
    if start_time and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', start_time):
        start_time = None

    description_parts = [
        clean_text(item.get(field))
        for field in ('excerpt', 'additionalInfo')
    ]
    description = '\n'.join(part for part in description_parts if part) or None
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, relative_url),
        'time_from': start_time,
        'venue': venue,
        'city': CITY,
        'country_code': 'DE',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(session, url):
    soup = get_soup(session, url)
    page = soup.select_one('#page-content')
    if not page:
        return None

    parts = []
    subtitle = page.select_one('.text-center p.fs-5')
    body = page.select_one('.container-extra-small .text-justify')
    for node in (subtitle, body):
        value = clean_text(node) if node else ''
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for year, month in published_months(session):
        try:
            items = month_items(session, year, month)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to scrape calendar month',
                event='crawler_page_failed',
                level='warning',
                url=f'{CALENDAR_URL}?year={year}&month={month}',
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        for item in items:
            record = item_record(item, year, month)
            if record:
                records.append(record)

    descriptions = {}
    urls = sorted({record['url'] for record in records})
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(detail_description, session, url): url for url in urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        detail = descriptions.get(record['url'])
        if detail:
            record['description'] = detail

    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class StadttheaterGiessenDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='stadttheater_giessen_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        dedupe_subset=['date', 'time_from', 'url', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    StadttheaterGiessenDeCrawler().run()


if __name__ == '__main__':
    main()
