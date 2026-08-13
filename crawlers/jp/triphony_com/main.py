import re
from datetime import date
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.triphony.com/'
CONCERT_URL = f'{SOURCE_URL}concert/'
SOURCE = 'Sumida Triphony Hall'
CITY = 'Tokyo'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja,en;q=0.8',
}

SKIP_TITLES = {
    '保守点検',
    'リハーサル',
    '公演予定',
    '公演予定（関係者のみ）',
    '大ホール公演同時利用',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u3000', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def listed_months(soup):
    months = set()
    for option in soup.select('select[name="sidecalendar"] option[value]'):
        query = parse_qs(urlparse(option['value']).query)
        try:
            year = int(query['y'][0])
            month = int(query['m'][0])
            date(year, month, 1)
        except (KeyError, TypeError, ValueError):
            continue
        months.add((year, month))
    return sorted(months)


def venue_name(raw_name):
    name = clean_text(raw_name)
    if name == '大ホール':
        return 'Sumida Triphony Hall - Main Hall'
    if name == '小ホール':
        return 'Sumida Triphony Hall - Recital Hall'
    return None


def make_record(item, year, month, page_url):
    title_node = item.select_one('.ttlConcert')
    title = clean_text(title_node)
    if not title or title in SKIP_TITLES:
        return None

    day_node = item.select_one('.txtDate')
    venue = venue_name(item.select_one('.txtHall'))
    anchor = item.select_one('.anchor[id]')
    if not day_node or not venue or not anchor:
        return None
    try:
        event_date = date(year, month, int(clean_text(day_node))).isoformat()
    except ValueError:
        return None

    time_text = clean_text(item.select_one('.txtTime'))
    time_match = re.search(r'([01]?\d|2[0-3]):([0-5]\d)', time_text)
    time_from = (
        f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
        if time_match else None
    )

    detail = item.select_one('.boxDetail')
    description = clean_text(detail) or None
    return {
        'title': title,
        'date': event_date,
        'url': f'{page_url}#{anchor["id"]}',
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'JP',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    landing = get_soup(session, CONCERT_URL)
    months = listed_months(landing)
    records = []

    for year, month in months:
        page_url = f'{CONCERT_URL}{year}{month:02d}list.html'
        try:
            soup = get_soup(session, page_url)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape concert month',
                event='crawler_page_failed',
                level='warning',
                url=page_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        for item in soup.select('li.boxAcc'):
            record = make_record(item, year, month, page_url)
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class TriphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='triphony_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
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
    TriphonyComCrawler().run()


if __name__ == '__main__':
    main()
