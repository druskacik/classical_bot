import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://tokyosymphony.jp/'
CONCERT_URL = urljoin(SOURCE_URL, 'concert/')
SOURCE = 'Tokyo Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

# The orchestra tours, so a Tokyo default would be unsafe. These hints cover
# the halls and municipalities represented in its catalogue; unknown locations
# are skipped rather than assigned a speculative city.
CITY_HINTS = {
    '東京': 'Tokyo', 'サントリーホール': 'Tokyo', 'オーチャードホール': 'Tokyo',
    '紀尾井ホール': 'Tokyo', '杉並公会堂': 'Tokyo', '文京シビック': 'Tokyo',
    'すみだトリフォニー': 'Tokyo', 'ミューザ川崎': 'Kawasaki', '川崎': 'Kawasaki',
    '横浜': 'Yokohama', '神奈川県立音楽堂': 'Yokohama',
    '横須賀': 'Yokosuka', 'よこすか': 'Yokosuka',
    '新潟': 'Niigata', '長岡': 'Nagaoka', '上越': 'Joetsu', '柏崎': 'Kashiwazaki',
    '仙台': 'Sendai', '福島': 'Fukushima', '郡山': 'Koriyama', 'いわき': 'Iwaki',
    '札幌': 'Sapporo', '函館': 'Hakodate', '青森': 'Aomori', '盛岡': 'Morioka',
    '山形': 'Yamagata', '長野': 'Nagano', '松本': 'Matsumoto', '軽井沢': 'Karuizawa',
    '富山': 'Toyama', '金沢': 'Kanazawa', '福井': 'Fukui', '甲府': 'Kofu',
    '静岡': 'Shizuoka', '浜松': 'Hamamatsu', '名古屋': 'Nagoya', '豊田': 'Toyota',
    '岐阜': 'Gifu', '京都': 'Kyoto', '大阪': 'Osaka', '堺': 'Sakai',
    '神戸': 'Kobe', '西宮': 'Nishinomiya', '奈良': 'Nara', '岡山': 'Okayama',
    '広島': 'Hiroshima', '高松': 'Takamatsu', '松山': 'Matsuyama',
    '福岡': 'Fukuoka', '熊本': 'Kumamoto', '鹿児島': 'Kagoshima', '沖縄': 'Okinawa',
    '所沢': 'Tokorozawa', '大宮': 'Saitama', 'さいたま': 'Saitama',
    '千葉': 'Chiba', '市川': 'Ichikawa', '船橋': 'Funabashi', '浦安': 'Urayasu',
    '水戸': 'Mito', '宇都宮': 'Utsunomiya', '高崎': 'Takasaki',
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
    return None


def discover_seasons(session):
    response = session.get(CONCERT_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    return [
        option['value'] for option in soup.select('select[name="season"] option[value]')
        if option['value'].isdigit() and option['value'] != '0'
    ]


def listing_urls(session, season):
    urls = set()
    page = 1
    while True:
        page_url = CONCERT_URL if page == 1 else urljoin(CONCERT_URL, f'page/{page}/')
        response = session.get(page_url, params={'season': season}, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        found = {
            link['href'] for link in soup.select('a[href]')
            if re.fullmatch(r'https://tokyosymphony\.jp/concert/\d+/', link.get('href', ''))
        }
        new_urls = found - urls
        if not new_urls:
            break
        urls.update(new_urls)
        next_page = f'/concert/page/{page + 1}/'
        if not any(next_page in link.get('href', '') for link in soup.select('a[href]')):
            break
        page += 1
    return urls


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    container = soup.select_one('.concertsdetails__contents')
    title = clean_text(soup.select_one('.concertsdetails__contents-title')).replace('\n', ' ')
    raw_date = clean_text(soup.select_one('.concertsdetails__contents-date'))
    raw_time = clean_text(soup.select_one('.concertsdetails__contents-time'))
    venue = clean_text(soup.select_one('.concertsdetails__contents-location')).replace('\n', ' ')
    match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', raw_date)
    city = resolve_city(venue)
    if not all((container, title, match, venue, city)):
        return None
    try:
        event_date = date(*map(int, match.groups())).isoformat()
    except ValueError:
        return None

    time_match = re.search(r'([0-2]?\d)\s*[:：]\s*([0-5]\d)', raw_time)
    time_from = None
    if time_match and int(time_match.group(1)) < 24:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    # Sidebars and ticket blocks add sales prose but no programme evidence.
    for node in container.select(
        '.wp-block-concert-block-ticket-frame, .wp-block-concert-block-ticket-price, '
        '.concertsdetails__side, script, style, nav, form'
    ):
        node.decompose()
    description = clean_text(container)
    return {
        'title': title, 'date': event_date, 'url': url, 'time_from': time_from,
        'venue': venue, 'city': city, 'country_code': 'JP',
        'description': description or None, 'source_url': SOURCE_URL, 'source': SOURCE,
    }


def fetch_detail(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return parse_detail(response.text, url)


class TokyoSymphonyJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tokyosymphony_jp', source=SOURCE, source_url=SOURCE_URL,
        country_code='JP', upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = set()
        for season in discover_seasons(session):
            try:
                urls.update(listing_urls(session, season))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Tokyo Symphony season listing',
                    event='crawler_listing_fetch_failed', level='warning',
                    url=CONCERT_URL, season=season, error_type=type(error).__name__,
                    error_message=str(error),
                )

        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(fetch_detail, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Tokyo Symphony concert detail',
                        event='crawler_detail_fetch_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    TokyoSymphonyJpCrawler().run()


if __name__ == '__main__':
    main()
