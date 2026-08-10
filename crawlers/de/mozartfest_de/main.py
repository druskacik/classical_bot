import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mozartfest.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'kalender-tickets/kalender-tickets/index.html')
SOURCE = 'Mozartfest Würzburg'

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
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
    )))
    return session


def archive_params():
    # The calendar's year selector begins at 1930. A broad query includes all
    # events which the site still retains, including completed festival dates.
    return {
        'ev[start][d]': '1',
        'ev[start][m]': '1',
        'ev[start][y]': '1930',
        'ev[end][d]': '31',
        'ev[end][m]': '12',
        'ev[end][y]': str(date.today().year + 2),
    }


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_items(session):
    soup = get_soup(session, CALENDAR_URL, archive_params())
    items = []
    for card in soup.select('.evresultlist .eventwrap'):
        link = card.select_one('a[href*="ev%5Bid%5D"], a[href*="ev[id]"]')
        match = re.search(r'(?:ev%5Bid%5D|ev\[id\])=(\d+)', link.get('href', '')) if link else None
        if match:
            items.append({'id': match.group(1), 'card': card})
    return items


def detail_url(event_id):
    params = archive_params()
    params['ev[id]'] = event_id
    return requests.Request('GET', CALENDAR_URL, params=params).prepare().url


def parse_datetime(value):
    date_match = re.search(r'(\d{2}\.\d{2}\.\d{4})', value)
    time_match = re.search(r'\|\s*(\d{1,2}:\d{2})\s*Uhr', value)
    if not date_match:
        return None, None
    day, month, year = map(int, date_match.group(1).split('.'))
    try:
        parsed_date = date(year, month, day).isoformat()
    except ValueError:
        return None, None
    return parsed_date, time_match.group(1).zfill(5) if time_match else None


def parse_detail(soup, url):
    detail = soup.select_one('.eventdetail')
    if not detail:
        return None
    title = clean_text(detail.select_one('.evheadline'))
    event_date, time_from = parse_datetime(clean_text(detail.select_one('.event-day')))
    venue = clean_text(detail.select_one('.event-location')).lstrip('|').strip()
    city = clean_text(detail.select_one('.gmaport'))
    description = clean_text(detail.select_one('.ev-content')) or None

    # Mozartfest is a Würzburg festival and unqualified calendar locations are
    # local. Tour entries, when present, carry their own address and gmaport.
    city = city or 'Würzburg'
    if not title or not event_date or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = make_session()
    items = listing_items(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        for item in items:
            url = detail_url(item['id'])
            futures[executor.submit(get_soup, session, url)] = url
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_detail(future.result(), url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Mozartfest concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['city'], item['title'], item['url']
    ))


class MozartfestDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mozartfest_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    MozartfestDeCrawler().run()


if __name__ == '__main__':
    main()
