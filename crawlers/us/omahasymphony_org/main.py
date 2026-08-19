import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.omahasymphony.org/'
API_URL = f'{SOURCE_URL}concerts.json'
SOURCE = 'Omaha Symphony'
DEFAULT_CITY = 'Omaha'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        return datetime.strptime(str(value), '%Y-%m-%d').date().isoformat()
    except (TypeError, ValueError):
        return ''


def parse_time(value):
    if not value:
        return None
    match = re.search(r'\b(\d{2}):(\d{2})\b', str(value))
    if not match:
        return None
    hour, minute = map(int, match.groups())
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def fetch_events(session):
    events = []
    page = 1
    while True:
        response = session.get(API_URL, params={'page': page}, timeout=45)
        response.raise_for_status()
        payload = response.json()
        groups = payload.get('data') or []
        events.extend(item for group in groups for item in group)

        pagination = (payload.get('meta') or {}).get('pagination') or {}
        total_pages = int(pagination.get('total_pages') or 1)
        if page >= total_pages:
            break
        page += 1
    return events


def detail_data(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    venue_node = soup.select_one('a[aria-label="venue"]')
    venue = clean_text(venue_node) if venue_node else ''

    description_parts = []
    intro = soup.select_one('.hero-text-detail-page .f19px')
    if intro:
        description_parts.append(clean_text(intro))
    for node in soup.select('.concert-info .paragraph'):
        text = clean_text(node)
        if text and text not in description_parts:
            description_parts.append(text)

    return venue, '\n\n'.join(description_parts) or None


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    events = fetch_events(session)
    details = {}
    records = []

    for event in events:
        title = clean_text(event.get('fullTitle') or event.get('title'))
        event_date = parse_date(event.get('date'))
        url = str(event.get('url') or '').strip()
        if not title or not event_date or not url.startswith(SOURCE_URL):
            continue

        if url not in details:
            try:
                details[url] = detail_data(session, url)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Concert detail request failed',
                    event='crawler_detail_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                details[url] = ('', None)

        venue, detail_description = details[url]
        if not venue:
            continue
        api_description = clean_text(event.get('description')) or None
        description = detail_description or api_description

        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(event.get('start')),
            'venue': venue,
            'city': DEFAULT_CITY,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    if not records:
        log_message(
            'No valid concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class OmahaSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='omahasymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        return scrape_concerts()


def main():
    OmahaSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
