import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bunkamura.co.jp/'
SOURCE = 'Bunkamura'
ARCHIVE_URL = f'{SOURCE_URL}archive/'
ARCHIVE_FIRST_YEAR = 1989

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

# Bunkamura's first-party archive has only the broad 公演 (performance)
# classification.  It contains classical concerts and ballet alongside plays,
# jazz, and popular music, so records go through potential-event classification.
ARCHIVE_GENRE = 'performance'


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\r', '\n').replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def resolve_location(raw_venue):
    venue = clean_text(BeautifulSoup(raw_venue or '', 'html.parser')).replace('\n', ' ')
    if not venue:
        return None

    if '世紀劇院' in venue or '北京' in venue:
        return venue, 'Beijing', 'CN'
    if 'EDINBURGH' in venue.upper() or 'エディンバラ' in venue:
        return venue, 'Edinburgh', 'GB'
    if 'ザ・シンフォニーホール' in venue:
        return venue, 'Osaka', 'JP'
    if any(token in venue for token in ('横浜', 'KAAT', '神奈川芸術劇場')):
        return venue, 'Yokohama', 'JP'

    # All remaining named archive venues are in Tokyo.  This includes
    # Bunkamura's two halls and the temporary Tokyo venues used during closure.
    tokyo_tokens = (
        'Bunkamura', 'MILANO-Za', 'セシオン杉並', '浜離宮朝日ホール',
        '世田谷パブリックシアター', 'めぐろパーシモンホール',
        '東京芸術劇場', '東京建物 Brillia HALL', 'IMM THEATER',
        '赤坂ACTシアター', 'にしすがも創造舎', '東京文化会館',
        '紀伊國屋ホール', '東京',
    )
    if any(token in venue for token in tokyo_tokens):
        return venue, 'Tokyo', 'JP'
    return None


def parse_times(raw_schedule):
    """Return start times keyed by ISO date from the human schedule field."""
    text = clean_text(BeautifulSoup(raw_schedule or '', 'html.parser'))
    result = {}
    date_pattern = re.compile(r'(\d{4})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})')
    matches = list(date_pattern.finditer(text))
    for index, match in enumerate(matches):
        try:
            event_date = date(*map(int, match.group(1, 2, 3))).isoformat()
        except ValueError:
            continue
        segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        time_match = re.search(
            r'(\d{1,2})\s*[:：]\s*([0-5]\d)\s*開演',
            text[match.end():segment_end],
        )
        hour, minute = time_match.groups() if time_match else (None, None)
        result[event_date] = (
            f'{int(hour):02d}:{minute}' if hour and int(hour) < 24 else None
        )
    return result


def parse_archive_item(item, year):
    if not item or item.get('genre') != ARCHIVE_GENRE:
        return []
    title = clean_text(item.get('title')).replace('\n', ' ')
    location = resolve_location(item.get('place_name'))
    if not title or not location:
        return []
    venue, city, country_code = location

    soup = BeautifulSoup(item.get('shosai') or '', 'html.parser')
    for node in soup.select('script, style, form'):
        node.decompose()
    description = clean_text(soup) or None
    times = parse_times(item.get('date'))
    dates = []
    for raw_date in re.findall(r'\d{4}-\d{2}-\d{2}', item.get('date_all') or ''):
        try:
            dates.append(date.fromisoformat(raw_date).isoformat())
        except ValueError:
            continue
    dates = list(dict.fromkeys(dates))

    key = quote(str(item.get('key') or item.get('id') or title), safe='')
    url = f'{ARCHIVE_URL}?year={year}&genre={ARCHIVE_GENRE}#{key}'
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': times.get(event_date),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
    ]


def fetch_archive_year(session, year):
    url = f'{SOURCE_URL}data/archive/{year}.json'
    response = session.get(url, timeout=60)
    if response.status_code == 404:
        return year, []
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(f'Unexpected archive payload for {year}')
    return year, payload


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    years = range(ARCHIVE_FIRST_YEAR, date.today().year + 1)
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_archive_year, session, year): year for year in years}
        for future in as_completed(futures):
            year = futures[future]
            try:
                _, items = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Bunkamura archive year',
                    event='crawler_archive_fetch_failed', level='warning',
                    url=f'{SOURCE_URL}data/archive/{year}.json',
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            for item in items:
                records.extend(parse_archive_item(item, year))
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class BunkamuraCoJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bunkamura_co_jp',
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
    BunkamuraCoJpCrawler().run()


if __name__ == '__main__':
    main()
