import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.kyoto-symphony.jp/'
SOURCE = 'City of Kyoto Symphony Orchestra'
API_URL = f'{SOURCE_URL}js/concert/ajax_get_concert-demo.php'
ARCHIVE_START_YEAR = 2008
FUTURE_MONTHS = 18

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
    'X-Requested-With': 'XMLHttpRequest',
}

# The orchestra is based in Kyoto, and its calendar predominantly contains
# Kyoto performances. Explicit tour-place names must override that default.
CITY_HINTS = {
    '京都': 'Kyoto', '大阪': 'Osaka', '東京': 'Tokyo', '横浜': 'Yokohama',
    '神戸': 'Kobe', '奈良': 'Nara', '大津': 'Otsu', '滋賀': 'Otsu',
    '宇治': 'Uji', '亀岡': 'Kameoka', '長岡京': 'Nagaokakyo',
    '八幡': 'Yawata', '舞鶴': 'Maizuru', '福知山': 'Fukuchiyama',
    '城陽': 'Joyo', '向日': 'Muko', '京田辺': 'Kyotanabe',
    '名古屋': 'Nagoya', '広島': 'Hiroshima', '福岡': 'Fukuoka',
    '金沢': 'Kanazawa', '札幌': 'Sapporo', '仙台': 'Sendai',
    '新潟': 'Niigata', '長野': 'Nagano', '松本': 'Matsumoto',
    '静岡': 'Shizuoka', '浜松': 'Hamamatsu', '岡山': 'Okayama',
    '高松': 'Takamatsu', '徳島': 'Tokushima', '熊本': 'Kumamoto',
}


def clean_html(value):
    if not value:
        return ''
    soup = BeautifulSoup(html.unescape(str(value)), 'html.parser')
    text = soup.get_text('\n', strip=True).replace('\u3000', ' ').replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    match = re.fullmatch(r'\s*(\d{1,2}):(\d{2})\s*([ap])m\s*', value or '', re.I)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour)
    if not 1 <= hour <= 12 or int(minute) > 59:
        return None
    hour = hour % 12 + (12 if meridiem.lower() == 'p' else 0)
    return f'{hour:02d}:{minute}'


def resolve_city(venue):
    for hint, city in CITY_HINTS.items():
        if hint in venue:
            return city
    # Venue names without a place name in this institution's own calendar are
    # local (e.g. Kita, Tobu, and Kuretake cultural halls).
    return 'Kyoto'


def month_pairs():
    today = date.today()
    end_index = today.year * 12 + today.month - 1 + FUTURE_MONTHS
    start_index = ARCHIVE_START_YEAR * 12
    return [(index // 12, index % 12 + 1) for index in range(start_index, end_index + 1)]


def fetch_month(year, month):
    response = requests.post(
        API_URL, data={'y': year, 'm': month, 'lang': ''},
        headers=HEADERS, timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get('detail', []) if payload.get('ret') else []


def parse_event(item):
    try:
        event_date = date(int(item['y']), int(item['m']), int(item['d'])).isoformat()
    except (KeyError, TypeError, ValueError):
        return None

    title = clean_html(item.get('name')).replace('\n', ' ')
    venue = clean_html(item.get('hall')).replace('\n', ' ')
    event_id = str(item.get('id') or '').strip()
    if not title or not venue or not event_id:
        return None

    sections = []
    for label, field in (
        ('指揮', 'conductor'), ('出演', 'costar'), ('プログラム', 'program'),
        ('備考', 'memo'),
    ):
        value = clean_html(item.get(field))
        if value:
            sections.append(f'{label}\n{value}')

    year, month = int(item['y']), int(item['m'])
    return {
        'title': title,
        'date': event_date,
        'url': f'{SOURCE_URL}concert/detail.php?id={event_id}&y={year}&m={month}',
        'time_from': parse_time(item.get('time_start')),
        'venue': venue,
        'city': resolve_city(venue),
        'country_code': 'JP',
        'description': '\n\n'.join(sections) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class KyotoSymphonyJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kyoto_symphony_jp',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(fetch_month, year, month): (year, month)
                for year, month in month_pairs()
            }
            for future in as_completed(futures):
                year, month = futures[future]
                try:
                    items = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to fetch Kyoto Symphony calendar month',
                        event='crawler_month_fetch_failed', level='warning',
                        url=API_URL, year=year, month=month,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                for item in items:
                    record = parse_event(item)
                    if record:
                        records.append(record)
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    KyotoSymphonyJpCrawler().run()


if __name__ == '__main__':
    main()
