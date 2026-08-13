import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://jcso.or.jp/'
SOURCE = 'Japan Century Symphony Orchestra'
CONCERT_URL = urljoin(SOURCE_URL, 'concert/')
PAST_INDEX_URL = urljoin(SOURCE_URL, 'pastconcert/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

# Explicit municipality names take priority over hall defaults. The orchestra
# tours, so an unknown venue is skipped rather than assigned its Osaka home city.
CITY_HINTS = {
    '豊中': 'Toyonaka', '吹田': 'Suita', '池田': 'Ikeda', '箕面': 'Minoh',
    '高槻': 'Takatsuki', '茨木': 'Ibaraki', '枚方': 'Hirakata', '東大阪': 'Higashiosaka',
    '八尾': 'Yao', '堺市': 'Sakai', '堺': 'Sakai', '大阪': 'Osaka',
    '西宮': 'Nishinomiya', '尼崎': 'Amagasaki', '神戸': 'Kobe', '加古川': 'Kakogawa',
    '姫路': 'Himeji', '宝塚': 'Takarazuka', '丹波': 'Tamba', '洲本': 'Sumoto',
    '京都': 'Kyoto', '宇治': 'Uji', '奈良': 'Nara', '大和高田': 'Yamatotakada',
    '大津': 'Otsu', 'びわ湖': 'Otsu', '和歌山': 'Wakayama',
    '周南': 'Shunan', '岩国': 'Iwakuni', '広島': 'Hiroshima', '岡山': 'Okayama',
    '倉敷': 'Kurashiki', '高松': 'Takamatsu', '徳島': 'Tokushima',
    '松山': 'Matsuyama', '高知': 'Kochi', '福岡': 'Fukuoka', '熊本': 'Kumamoto',
    '長崎': 'Nagasaki', '大分': 'Oita', '宮崎': 'Miyazaki', '鹿児島': 'Kagoshima',
    '名古屋': 'Nagoya', '豊田': 'Toyota', '岐阜': 'Gifu', '津市': 'Tsu',
    '浜松': 'Hamamatsu', '静岡': 'Shizuoka', '金沢': 'Kanazawa', '福井': 'Fukui',
    '長野': 'Nagano', '松本': 'Matsumoto', '新潟': 'Niigata',
    '横浜': 'Yokohama', '川崎': 'Kawasaki', 'さいたま': 'Saitama',
    '千葉': 'Chiba', '東京': 'Tokyo', '渋谷': 'Tokyo', '池袋': 'Tokyo',
    '札幌': 'Sapporo', '仙台': 'Sendai',
}

HALL_CITIES = {
    'ザ・シンフォニーホール': 'Osaka', 'フェスティバルホール': 'Osaka',
    '住友生命いずみホール': 'Osaka', 'いずみホール': 'Osaka',
    'オリックス劇場': 'Osaka', 'NHK大阪ホール': 'Osaka',
    '服部緑地野外音楽堂': 'Toyonaka', 'センチュリー・オーケストラハウス': 'Toyonaka',
    '兵庫県立芸術文化センター': 'Nishinomiya', '神戸国際会館': 'Kobe',
    'ロームシアター京都': 'Kyoto', '京都コンサートホール': 'Kyoto',
    'びわ湖ホール': 'Otsu', 'サントリーホール': 'Tokyo',
    '東京芸術劇場': 'Tokyo', '東京オペラシティ': 'Tokyo', 'NHKホール': 'Tokyo',
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


def archive_pages(session):
    """Return the current feed and every first-party yearly archive page."""
    response = session.get(PAST_INDEX_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    pages = [CONCERT_URL]
    for link in soup.select('a[href*="/concert/tag/"]'):
        url = urljoin(PAST_INDEX_URL, link.get('href'))
        if re.search(r'/concert/tag/(?:regular|special|others)\d{4}/?$', url):
            pages.append(url)
    return list(dict.fromkeys(pages))


def detail_urls(session):
    pending = archive_pages(session)
    seen_pages = set()
    details = []
    while pending:
        page_url = pending.pop(0)
        if page_url in seen_pages:
            continue
        seen_pages.add(page_url)
        response = session.get(page_url, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        for link in soup.select('a[href]'):
            url = urljoin(page_url, link.get('href'))
            if re.fullmatch(r'https://jcso\.or\.jp/concert/\d+/', url):
                details.append(url)
            elif re.fullmatch(r'https://jcso\.or\.jp/concert/(?:page/\d+/|tag/[^/]+/page/\d+/)', url):
                pending.append(url)
    return list(dict.fromkeys(details))


def event_description(container):
    performer = container.select_one('.p-concertPerformer')
    parts = []
    if performer:
        value = clean_text(performer)
        if value:
            parts.append(value)
        nodes = performer.find_next_siblings()
    else:
        venue = container.select_one('.p-concertVenue')
        nodes = venue.find_next_siblings() if venue else []
    for node in nodes:
        classes = set(node.get('class') or [])
        if classes & {'p-concertFree', 'p-concertButton', 'editor-area'}:
            break
        if node.name in {'p', 'div', 'ul', 'ol', 'dl'}:
            value = clean_text(node)
            if value and value not in parts:
                parts.append(value)
    return '\n\n'.join(parts) or None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    container = soup.select_one('.p-section .l-container')
    if not container:
        return None
    title = clean_text(container.select_one('.p-concertHead')).replace('\n', ' ')
    date_node = container.select_one('.p-concertDate time[datetime]')
    venue = clean_text(container.select_one('.p-concertVenue'))
    city = resolve_city(venue)
    if not all((title, date_node, venue, city)):
        return None
    try:
        event_date = date.fromisoformat(date_node.get('datetime', '')).isoformat()
    except ValueError:
        return None
    raw_time = clean_text(container.select_one('.p-concertTime'))
    time_match = re.search(r'(?<!\d)([0-2]?\d)\s*[:：]\s*([0-5]\d)\s*開演', raw_time)
    time_from = None
    if time_match and int(time_match.group(1)) < 24:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
    return {
        'title': title, 'date': event_date, 'url': url, 'time_from': time_from,
        'venue': venue, 'city': city, 'country_code': 'JP',
        'description': event_description(container),
        'source_url': SOURCE_URL, 'source': SOURCE,
    }


def fetch_detail(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return parse_detail(response.text, url)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = detail_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_detail, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape JCSO concert detail',
                    event='crawler_detail_fetch_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class JcsoOrJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jcso_or_jp', source=SOURCE, source_url=SOURCE_URL,
        country_code='JP', upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    JcsoOrJpCrawler().run()


if __name__ == '__main__':
    main()
