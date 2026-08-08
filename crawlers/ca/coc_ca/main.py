import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.coc.ca/'
SOURCE = 'Canadian Opera Company'
EVENTS_API = 'https://d1ndd0kfyiplr2.cloudfront.net/Prod/events/10/40/Live'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Content-Type': 'application/json',
    'Accept-Language': 'en-CA,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    lines = [' '.join(line.split()) for line in text.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def city_from_venue(venue):
    lines = [
        clean_text(venue.get(field))
        for field in ('AddressLineOne', 'AddressLineTwo')
        if venue.get(field)
    ]
    # The API currently formats Canadian addresses as either
    # "Toronto, ON M5H 4G1" or "239 Front St E, Toronto ON M5A 1E8".
    for line in lines:
        match = re.match(
            r'([A-Za-z][A-Za-z .\'-]+?),?\s+[A-Z]{2}\s+[A-Z]\d[A-Z]\s*\d[A-Z]\d\b',
            line,
        )
        if match:
            return clean_text(match.group(1))
    if len(lines) > 1 and re.match(r'^[A-Z]{2}\b', lines[1]):
        match = re.search(r',\s*([A-Za-z][A-Za-z .\'-]+)$', lines[0])
        if match:
            return clean_text(match.group(1))
    return ''


def record_description(event):
    parts = [clean_text(event.get('Summary')), clean_text(event.get('Suffix'))]
    return '\n\n'.join(dict.fromkeys(part for part in parts if part)) or None


def make_records(event):
    title = clean_text(event.get('Title'))
    link = clean_text(event.get('Link'))
    venue_data = event.get('Venue') or {}
    venue = clean_text(venue_data.get('Title'))
    city = city_from_venue(venue_data)
    url = urljoin(SOURCE_URL, link)
    if not title or not link or not venue or not city:
        log_message(
            'Skipped event with incomplete required fields',
            event='crawler_item_skipped',
            level='warning',
            url=url,
        )
        return []

    records = []
    for performance in event.get('Performances') or []:
        production_type = performance.get('ProductionType') or {}
        # Match the COC frontend: dress and working rehearsals are not public
        # performances in its event calendar.
        if production_type.get('ID') in (4, 5):
            continue
        starts_at = parse_datetime(performance.get('PerformanceDate'))
        if starts_at is None:
            continue
        records.append(
            {
                'title': title,
                'date': starts_at.date().isoformat(),
                'url': url,
                'time_from': starts_at.strftime('%H:%M'),
                'venue': venue,
                'city': city,
                'country_code': 'CA',
                'description': record_description(event),
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )
    return records


def detail_description(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    content = soup.select_one('#content-blocks')
    if content:
        for unrelated in content.select('.event-scroller, .image-gallery'):
            unrelated.decompose()
    return clean_text(content) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    response = session.get(EVENTS_API, timeout=60)
    response.raise_for_status()
    events = response.json()

    records = []
    for event in events:
        records.extend(make_records(event))

    descriptions = {}
    urls = {record['url'] for record in records}
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
        if descriptions.get(record['url']):
            record['description'] = descriptions[record['url']]

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class CocCaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='coc_ca',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CA',
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
    CocCaCrawler().run()


if __name__ == '__main__':
    main()
