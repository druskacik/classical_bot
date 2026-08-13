import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.kawasaki-sym-hall.jp/'
SOURCE = 'MUZA Kawasaki Symphony Hall'
CITY = 'Kawasaki'
LISTING_API = f'{SOURCE_URL}contents/performance'
HISTORY_API = f'{LISTING_API}/history'
FIRST_ARCHIVE_YEAR = 2004

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_text(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response.text


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def month_range():
    """Cover the current calendar and the site's two-year booking horizon."""
    today = date.today()
    end_year = today.year + 2
    for year in range(today.year, end_year + 1):
        for month in range(1, 13):
            if year == today.year and month < today.month:
                continue
            if year == end_year and month > today.month:
                return
            yield year, month


def parse_listing(html, year, month):
    soup = BeautifulSoup(html, 'html.parser')
    items = []
    for link in soup.select('a.eventlink[href]'):
        title = clean_text(link.select_one('.title')).replace('\n', ' ')
        venue = clean_text(link.select_one('.venue .name')).replace('\n', ' ')
        day_text = clean_text(link.select_one('.date .day'))
        if not title or not venue or not day_text.isdigit():
            continue
        try:
            event_date = date(year, month, int(day_text)).isoformat()
        except ValueError:
            continue
        time_match = re.search(r'([0-2]?\d):([0-5]\d)', clean_text(link.select_one('.time')))
        time_from = None
        if time_match and int(time_match.group(1)) < 24:
            time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
        url = urljoin(SOURCE_URL, link.get('href'))
        event_id = parse_qs(urlparse(url).query).get('id', [None])[0]
        items.append({
            'title': title, 'date': event_date, 'url': url,
            'time_from': time_from, 'venue': venue, 'event_id': event_id,
        })
    return items


def parse_history(html):
    soup = BeautifulSoup(html, 'html.parser')
    items = []
    for link in soup.select('a.eventlink[href]'):
        heading = link.find_previous('h2')
        month_match = re.search(r'(\d{4})\.(\d{1,2})月', clean_text(heading))
        if not month_match:
            continue
        items.extend(parse_listing(str(link), *map(int, month_match.groups())))
    return items


def listing_items(session):
    items = {}
    # This is the site's complete published archive. It contains MUZA-hosted
    # events from both the symphony hall and citizen exchange room by fiscal year.
    for year in range(FIRST_ARCHIVE_YEAR, date.today().year + 1):
        html = get_text(session, HISTORY_API, params={'year': year, 'lang': 'ja'})
        for item in parse_history(html):
            key = (item['url'], item['date'], item['time_from'])
            items[key] = item

    for year, month in month_range():
        for hall in (1, 2):
            html = get_text(
                session, LISTING_API,
                params={'year': year, 'month': month, 'lang': 'ja', 'hall': hall},
            )
            for item in parse_listing(html, year, month):
                key = (item['url'], item['date'], item['time_from'])
                items[key] = item
    return list(items.values())


def detail_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    parts = []
    for selector in ('.artist', '.program', '.outline'):
        text = clean_text(soup.select_one(selector))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def make_record(item, detail_html=None):
    description = detail_description(detail_html) if detail_html else None
    return {
        'title': item['title'],
        'date': item['date'],
        'url': item['url'],
        'time_from': item['time_from'],
        'venue': item['venue'],
        'city': CITY,
        'country_code': 'JP',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = listing_items(session)
    records = []

    def fetch_detail(item):
        if not item['event_id']:
            return make_record(item)
        namespace = 'festa/performance' if '/festa/' in item['url'] else 'performance'
        payload = get_json(
            session, f'{SOURCE_URL}contents/{namespace}/{item["event_id"]}',
            params={'lang': 'ja'},
        )
        return make_record(item, payload.get('content') or '')

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch_detail, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                records.append(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape MUZA concert detail',
                    event='crawler_detail_fetch_failed', level='warning',
                    url=item['url'], error_type=type(error).__name__,
                    error_message=str(error),
                )
                records.append(make_record(item))

    return sorted(records, key=lambda record: (
        record['date'], record['time_from'] or '', record['title'], record['url']
    ))


class KawasakiSymHallJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kawasaki_sym_hall_jp',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
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
    KawasakiSymHallJpCrawler().run()


if __name__ == '__main__':
    main()
