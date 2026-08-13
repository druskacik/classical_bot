import re
from datetime import date
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lfj.jp/lfj_2026/'
PERFORMANCE_URL = urljoin(SOURCE_URL, 'performance/performance.html')
SOURCE = 'La Folle Journee TOKYO 2026'
CITY = 'Tokyo'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_performance(box):
    number_node = box.select_one('.box_performance_title h3 em')
    title_node = box.select_one('.perfonmance_name')
    datetime_node = box.select_one('.performance_date_time')
    venue_node = box.select_one('.place_name')
    number = clean_text(number_node)
    title = clean_text(title_node).replace('\n', ' ')
    raw_datetime = clean_text(datetime_node)
    venue = re.sub(r'^[●・\s]+', '', clean_text(venue_node).replace('\n', ' '))

    date_match = re.search(r'(\d{1,2})月(\d{1,2})日', raw_datetime)
    time_match = re.search(r'([0-2]?\d):([0-5]\d)\s*[〜～~-]', raw_datetime)
    if not all((number, title, date_match, venue)):
        return None
    try:
        event_date = date(2026, int(date_match.group(1)), int(date_match.group(2))).isoformat()
    except ValueError:
        return None

    time_from = None
    if time_match and int(time_match.group(1)) < 24:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    description_parts = []
    music = clean_text(box.select_one('.music_name'))
    performers = clean_text(box.select_one('.performer_name'))
    if music:
        description_parts.append(f'曲目\n{music}')
    if performers:
        description_parts.append(f'出演者\n{performers}')

    detail_node = box.select_one('a[href*="/timetable/detail/"]')
    detail_url = urljoin(PERFORMANCE_URL, detail_node.get('href')) if detail_node else ''
    if not detail_url:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': detail_url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'JP',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    seen_numbers = set()
    page = 1

    while True:
        params = {'end': '1', 'page': page}
        url = f'{PERFORMANCE_URL}?{urlencode(params)}'
        try:
            response = session.get(url, timeout=60)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch LFJ performance page',
                event='crawler_page_fetch_failed', level='warning', url=url,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        boxes = soup.select('.box_performance')
        if not boxes:
            break

        new_on_page = 0
        for box in boxes:
            number = clean_text(box.select_one('.box_performance_title h3 em'))
            if not number or number in seen_numbers:
                continue
            seen_numbers.add(number)
            new_on_page += 1
            record = parse_performance(box)
            if record:
                records.append(record)

        pager_numbers = [
            int(node.get('data-page'))
            for node in soup.select('a[data-page]')
            if (node.get('data-page') or '').isdigit()
        ]
        if not new_on_page or not pager_numbers or page >= max(pager_numbers):
            break
        page += 1

    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class LfjJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lfj_jp',
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
    LfjJpCrawler().run()


if __name__ == '__main__':
    main()
