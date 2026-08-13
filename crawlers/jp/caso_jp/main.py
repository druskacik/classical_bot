import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.caso.jp/'
SOURCE = 'Central Aichi Symphony Orchestra'

# The current page contains every current/future category. Historical events are
# split across the four first-party concert-type archives.
LISTING_URLS = (
    f'{SOURCE_URL}concert/',
    f'{SOURCE_URL}concert/past-regular/',
    f'{SOURCE_URL}concert/past-special/',
    f'{SOURCE_URL}concert/past-public/',
    f'{SOURCE_URL}concert/past-chamber/',
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

# Venue names in the archive generally identify their municipality. Keep this
# explicit so a Nagoya home-city default cannot be applied to touring concerts.
CITY_HINTS = {
    '名古屋': 'Nagoya',
    '愛知県芸術劇場': 'Nagoya',
    '愛知芸術文化センター': 'Nagoya',
    'しらかわホール': 'Nagoya',
    '電気文化会館': 'Nagoya',
    'HITOMI': 'Nagoya',
    'メニコン': 'Nagoya',
    '宗次ホール': 'Nagoya',
    '文化小劇場': 'Nagoya',
    'アートピアホール': 'Nagoya',
    '茶屋が坂ホール': 'Nagoya',
    'Halle Runde': 'Nagoya',
    'ドルチェ・アートホール': 'Nagoya',
    '金城学院大学': 'Nagoya',
    'Zepp Nagoya': 'Nagoya',
    '上社レクリエーションルーム': 'Nagoya',
    '岩倉': 'Iwakura',
    '稲沢': 'Inazawa',
    '半田': 'Handa',
    '雁宿ホール': 'Handa',
    '東海市': 'Tokai',
    '刈谷': 'Kariya',
    '四日市': 'Yokkaichi',
    '可児': 'Kani',
    '三重県文化会館': 'Tsu',
    '高山市': 'Takayama',
    'アクトシティ浜松': 'Hamamatsu',
    '大府': 'Obu',
    '愛三文化会館': 'Obu',
    '碧南': 'Hekinan',
    '東郷町': 'Togo',
    '敦賀': 'Tsuruga',
    '岡山': 'Okayama',
    '豊橋': 'Toyohashi',
    '安城': 'Anjo',
    '高浜町': 'Takahama',
    'サラマンカホール': 'Gifu',
    '美浜町': 'Mihama',
    '春日井': 'Kasugai',
    '石川県立音楽堂': 'Kanazawa',
    '小浜市': 'Obama',
    '木之本スティックホール': 'Nagahama',
    '東京国際フォーラム': 'Tokyo',
    '東京オペラシティ': 'Tokyo',
    'NagoyaNoritakeGarden': 'Nagoya',
    'Niterra日本特殊陶業市民会館': 'Nagoya',
    '日本特殊陶業市民会館': 'Nagoya',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def resolve_city(venue):
    for hint, city in CITY_HINTS.items():
        if hint in venue:
            return city
    return None


def field_rows(section):
    rows = {}
    for row in section.select('tr'):
        heading = row.select_one('th')
        value = row.select_one('td')
        if heading and value:
            rows[clean_text(heading)] = value
    return rows


def extract_occurrences(raw_date):
    date_pattern = re.compile(
        r'(?:(\d{4})年\s*)?(?:(\d{1,2})月\s*)?(\d{1,2})日'
    )
    matches = list(date_pattern.finditer(raw_date))
    if not matches:
        return []
    # A bracketed older date described as a rescheduled performance is not a
    # second occurrence.
    if '振替公演' in raw_date:
        matches = matches[:1]

    year = month = None
    occurrences = []
    for index, match in enumerate(matches):
        year = int(match.group(1)) if match.group(1) else year
        month = int(match.group(2)) if match.group(2) else month
        if year is None or month is None:
            continue
        try:
            event_date = date(year, month, int(match.group(3))).isoformat()
        except ValueError:
            continue

        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw_date)
        segment = raw_date[match.end():end]
        times = re.findall(r'([0-2]?\d)\s*[：:]\s*([0-5]\d)\s*開演', segment)
        if not times:
            times = re.findall(r'開演\s*([0-2]?\d)\s*[：:]\s*([0-5]\d)', segment)
        generic_segment = re.split(r'[※（(]開場', segment, maxsplit=1)[0]
        if not times and '開場' not in generic_segment and '受付' not in generic_segment:
            times = re.findall(
                r'([0-2]?\d)\s*[：:]\s*([0-5]\d)', generic_segment
            )
        valid_times = [
            f'{int(hour):02d}:{minute}' for hour, minute in times if int(hour) < 24
        ]
        if not valid_times:
            for period, hour_text, minute_text in re.findall(
                r'(午前|午後)\s*(\d{1,2})時(?:(\d{1,2})分)?', generic_segment
            ):
                hour = int(hour_text) % 12 + (12 if period == '午後' else 0)
                valid_times.append(f'{hour:02d}:{int(minute_text or 0):02d}')
        for event_time in dict.fromkeys(valid_times or [None]):
            occurrences.append((event_date, event_time))
    return occurrences


def parse_section(section, listing_url):
    heading = section.select_one('h2')
    rows = field_rows(section)
    title = clean_text(heading).replace('\n', ' ')
    raw_date = clean_text(rows.get('日時'))
    venue = clean_text(rows.get('会場')).replace('\n', ' ')
    city = resolve_city(venue)
    occurrences = extract_occurrences(raw_date)
    if not all((title, occurrences, venue, city)):
        return []

    description_parts = []
    for label in ('出演', 'プログラム', '備考'):
        text = clean_text(rows.get(label))
        if text:
            description_parts.append(f'{label}\n{text}')

    section_id = section.get('id', '').strip()
    url = f'{listing_url}#{section_id}' if section_id else listing_url
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'JP',
            'description': '\n\n'.join(description_parts) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from in occurrences
    ]


def fetch_listing(url):
    response = requests.get(url, headers=HEADERS, timeout=120)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    return [
        record
        for section in soup.select('.concertSection')
        for record in parse_section(section, url)
    ]


def get_concerts():
    records = []
    with ThreadPoolExecutor(max_workers=len(LISTING_URLS)) as executor:
        futures = {executor.submit(fetch_listing, url): url for url in LISTING_URLS}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape CASO concert listing',
                    event='crawler_listing_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class CasoJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='caso_jp',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
        # The calendar includes lectures and ticket initiatives alongside
        # performances, so records require the potential-event classifier.
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
    CasoJpCrawler().run()


if __name__ == '__main__':
    main()
