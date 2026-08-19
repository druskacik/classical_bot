import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.musicinthevineyards.org/'
EVENTS_URL = f'{SOURCE_URL}events/'
SOURCE = 'Music in the Vineyards'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

# Events Manager otherwise defaults to upcoming events. Supplying an explicit
# range also exposes the site's still-published archive.
SEARCH_DATA = {
    'action': 'search_events',
    'view_id': '1',
    'scope[0]': '2000-01-01',
    'scope[1]': '2100-12-31',
    'view': 'list',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    value = re.sub(r'(?<=\d)(?:st|nd|rd|th)', '', clean_text(value), flags=re.I)
    try:
        return datetime.strptime(value, '%A, %B %d, %Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(value):
    match = re.search(r'\b(\d{1,2}(?::\d{2})?\s*[ap]m)\b', clean_text(value), re.I)
    if not match:
        return None
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(match.group(1).upper(), pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_city(where):
    if not where:
        return ''
    text = clean_text(where.get_text('\n', strip=True))
    match = re.search(r'(?:^|\n)([^\n,]+),\s*CA\s+\d{5}(?:-\d{4})?(?:$|\n)', text, re.I)
    city = clean_text(match.group(1)) if match else ''
    return 'St. Helena' if city == 'St Helena' else city


def listing_items(session):
    # The first GET establishes the cookies expected by the site's cache/WAF.
    response = session.get(EVENTS_URL, timeout=45)
    response.raise_for_status()

    items = []
    for page_number in range(1, 101):
        response = session.post(
            EVENTS_URL,
            params={'pno': page_number},
            data=SEARCH_DATA,
            headers={'Referer': EVENTS_URL},
            timeout=45,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        cards = soup.select('.em-event.em-item')
        if not cards:
            break

        for card in cards:
            title_link = card.select_one('.em-item-title a[href]')
            date_node = card.select_one('.em-event-date')
            venue_link = card.select_one('.em-event-location a')
            if not title_link or not date_node or not venue_link:
                continue
            items.append({
                'title': clean_text(title_link.get_text(' ', strip=True)),
                'date': parse_date(date_node.get_text(' ', strip=True)),
                'url': title_link.get('href', '').strip(),
                'time_from': parse_time(
                    card.select_one('.em-event-time').get_text(' ', strip=True)
                    if card.select_one('.em-event-time') else ''
                ),
                'venue': clean_text(venue_link.get_text(' ', strip=True)),
                'listing_description': clean_text(
                    card.select_one('.em-item-desc').get_text('\n', strip=True)
                    if card.select_one('.em-item-desc') else ''
                ),
            })
    else:
        log_message(
            'Event pagination limit reached',
            event='crawler_pagination_limit',
            level='warning',
            url=EVENTS_URL,
        )

    return items


def fetch_detail(item):
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount(
        'https://',
        HTTPAdapter(max_retries=Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=('GET',),
        )),
    )
    try:
        response = session.get(item['url'], timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Event detail request failed',
            event='crawler_detail_failed',
            level='warning',
            url=item['url'],
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    soup = BeautifulSoup(response.text, 'html.parser')
    where = soup.select_one('.em-event-where')
    city = parse_city(where)
    content = soup.select_one('.em-event-content')
    description = clean_text(content.get_text('\n', strip=True)) if content else ''

    if not all((item['title'], item['date'], item['url'], item['venue'], city)):
        return None

    return {
        'title': item['title'],
        'date': item['date'],
        'url': item['url'],
        'time_from': item['time_from'],
        'venue': item['venue'],
        'city': city,
        'country_code': 'US',
        'description': description or item['listing_description'] or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    items = listing_items(session)

    records = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch_detail, item) for item in items]
        for future in as_completed(futures):
            record = future.result()
            if record:
                records.append(record)

    if not records:
        log_message(
            'No parseable events found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )

    unique = {
        (item['title'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    return sorted(unique.values(), key=lambda item: (item['date'], item['title'], item['url']))


class MusicInTheVineyardsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musicinthevineyards_org',
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
    MusicInTheVineyardsOrgCrawler().run()


if __name__ == '__main__':
    main()
