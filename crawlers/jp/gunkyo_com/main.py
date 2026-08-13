import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.gunkyo.com/'
SOURCE = 'Gunma Symphony Orchestra'
CONCERTS_API = f'{SOURCE_URL}wp-json/wp/v2/concerts'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

# The calendar includes touring engagements, so a home-city default would be
# unsafe. Most Japanese venue names identify their municipality; the hall
# aliases below cover recurring venues whose names do not.
CITY_HINTS = {
    '高崎': 'Takasaki', '前橋': 'Maebashi', '桐生': 'Kiryu',
    '太田': 'Ota', '伊勢崎': 'Isesaki', '館林': 'Tatebayashi',
    '渋川': 'Shibukawa', '藤岡': 'Fujioka', '富岡': 'Tomioka',
    '安中': 'Annaka', 'みどり市': 'Midori', '沼田': 'Numata',
    '草津': 'Kusatsu', '玉村': 'Tamamura', '大泉町': 'Oizumi',
    '邑楽町': 'Ora', '千代田町': 'Chiyoda', '明和町': 'Meiwa',
    '板倉町': 'Itakura', '吉岡町': 'Yoshioka', '榛東村': 'Shinto',
    '甘楽町': 'Kanra', '下仁田町': 'Shimonita', '中之条町': 'Nakanojo',
    '長野原町': 'Naganohara', '東吾妻町': 'Higashiagatsuma',
    '東京': 'Tokyo', '渋谷': 'Tokyo', '新宿': 'Tokyo', '上野': 'Tokyo',
    '横浜': 'Yokohama', 'さいたま': 'Saitama', '宇都宮': 'Utsunomiya',
    '足利': 'Ashikaga', '佐野': 'Sano', '軽井沢': 'Karuizawa',
}

HALL_CITIES = {
    '高崎芸術劇場': 'Takasaki', '群馬音楽センター': 'Takasaki',
    '昌賢学園まえばしホール': 'Maebashi', 'ベイシア文化ホール': 'Maebashi',
    '美喜仁桐生文化会館': 'Kiryu', 'メガネのイタガキ文化ホール伊勢崎': 'Isesaki',
    '東京芸術劇場': 'Tokyo', 'すみだトリフォニーホール': 'Tokyo',
    'サントリーホール': 'Tokyo', 'オーチャードホール': 'Tokyo',
}


def clean_text(value):
    if not value:
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
    for hint, city in HALL_CITIES.items():
        if hint in venue:
            return city
    return None


def listing_urls(session):
    urls = []
    page = 1
    while True:
        response = session.get(
            CONCERTS_API,
            params={'per_page': 100, 'page': page, '_fields': 'id,link'},
            timeout=60,
        )
        response.raise_for_status()
        urls.extend(item['link'] for item in response.json() if item.get('link'))
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return list(dict.fromkeys(urls))


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    detail = soup.select_one('.p-concerts-detail')
    if not detail:
        return None

    title = clean_text(detail.select_one('.p-concerts-detail__heading .title')).replace('\n', ' ')
    raw_date = clean_text(detail.select_one('p.date'))
    raw_time = clean_text(detail.select_one('p.hour'))
    venue = clean_text(detail.select_one('p.place')).replace('\n', ' ')
    city = resolve_city(venue)

    match = re.search(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日', raw_date)
    if not all((title, match, venue, city)):
        return None
    try:
        event_date = date(*map(int, match.groups())).isoformat()
    except ValueError:
        return None

    time_match = re.search(r'開演\s*[:：]?\s*([0-2]?\d)\s*[:：]\s*([0-5]\d)', raw_time)
    time_from = None
    if time_match and int(time_match.group(1)) < 24:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    description_parts = []
    for selector in ('#performer', '#program'):
        text = clean_text(detail.select_one(selector))
        if text:
            description_parts.append(text)

    return {
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


def fetch_detail(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return parse_detail(response.text, url)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fetch_detail, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Gunma Symphony Orchestra concert detail',
                    event='crawler_detail_fetch_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class GunkyoComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gunkyo_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    GunkyoComCrawler().run()


if __name__ == '__main__':
    main()
