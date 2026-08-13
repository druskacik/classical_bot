import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://kansaiphil.jp/'
SOURCE = 'Kansai Philharmonic Orchestra'
CALENDAR_API = f'{SOURCE_URL}wordpress/wp-admin/admin-ajax.php'
FIRST_ARCHIVE_YEAR = 2016

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

# The orchestra tours, so there is deliberately no blanket Osaka default.
# Municipality text takes priority; the hall table covers common venues whose
# published name does not contain a city.
CITY_HINTS = {
    '大阪市': 'Osaka', '大阪': 'Osaka', '東大阪': 'Higashiosaka',
    '門真': 'Kadoma', '枚方': 'Hirakata', '豊中': 'Toyonaka',
    '高槻': 'Takatsuki', '吹田': 'Suita', '茨木': 'Ibaraki',
    '八尾': 'Yao', '堺市': 'Sakai', '河内長野': 'Kawachinagano',
    '岸和田': 'Kishiwada', '泉佐野': 'Izumisano', '和泉市': 'Izumi',
    '寝屋川': 'Neyagawa', '守口': 'Moriguchi', '富田林': 'Tondabayashi',
    '神戸': 'Kobe', '西宮': 'Nishinomiya', '尼崎': 'Amagasaki',
    '姫路': 'Himeji', '宝塚': 'Takarazuka', '伊丹': 'Itami',
    '加古川': 'Kakogawa', '豊岡': 'Toyooka', '淡路': 'Awaji',
    '京都市': 'Kyoto', '京都': 'Kyoto', '城陽': 'Joyo', '宇治': 'Uji',
    '長岡京': 'Nagaokakyo', '福知山': 'Fukuchiyama', '舞鶴': 'Maizuru',
    '奈良市': 'Nara', '奈良': 'Nara', '橿原': 'Kashihara',
    '大和高田': 'Yamatotakada', '生駒': 'Ikoma',
    '和歌山市': 'Wakayama', '和歌山': 'Wakayama',
    '大津': 'Otsu', '彦根': 'Hikone', '草津': 'Kusatsu',
    '近江八幡': 'Omihachiman', '守山': 'Moriyama',
    '東京': 'Tokyo', '横浜': 'Yokohama', '川崎': 'Kawasaki',
    '名古屋': 'Nagoya', '豊田': 'Toyota', '岐阜': 'Gifu',
    '津市': 'Tsu', '四日市': 'Yokkaichi', '福井': 'Fukui',
    '金沢': 'Kanazawa', '岡山': 'Okayama', '倉敷': 'Kurashiki',
    '広島': 'Hiroshima', '徳島': 'Tokushima', '高松': 'Takamatsu',
    '松山': 'Matsuyama', '福岡': 'Fukuoka', '熊本': 'Kumamoto',
}

HALL_CITIES = {
    'ザ・シンフォニーホール': 'Osaka',
    'フェスティバルホール': 'Osaka',
    '住友生命いずみホール': 'Osaka',
    'いずみホール': 'Osaka',
    'オリックス劇場': 'Osaka',
    'NHK大阪ホール': 'Osaka',
    '大阪城ホール': 'Osaka',
    '大阪市中央公会堂': 'Osaka',
    '東大阪市文化創造館': 'Higashiosaka',
    'ルミエールホール': 'Kadoma',
    '枚方市総合文化芸術センター': 'Hirakata',
    '豊中市立文化芸術センター': 'Toyonaka',
    '兵庫県立芸術文化センター': 'Nishinomiya',
    '神戸国際会館': 'Kobe',
    '神戸文化ホール': 'Kobe',
    'ロームシアター京都': 'Kyoto',
    '京都コンサートホール': 'Kyoto',
    '文化パルク城陽': 'Joyo',
    'けいはんなプラザ': 'Seika',
    '奈良県文化会館': 'Nara',
    'びわ湖ホール': 'Otsu',
    '東京オペラシティ': 'Tokyo',
    'サントリーホール': 'Tokyo',
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
    for hall, city in HALL_CITIES.items():
        if hall in venue:
            return city
    return None


def calendar_entries(session):
    entries = {}
    # The calendar begins in 2016. Query complete calendar years because the
    # endpoint's start/end bounds are stable and return every matching post.
    for year in range(FIRST_ARCHIVE_YEAR, date.today().year + 4):
        response = session.get(
            CALENDAR_API,
            params={
                'action': 'WP_FullCalendar',
                'type': 'concert',
                'post_type': 'concert',
                'posts_per_page': 12,
                'start': f'{year}-01-01',
                'end': f'{year + 1}-01-01',
            },
            timeout=60,
        )
        response.raise_for_status()
        for item in response.json():
            url = item.get('url')
            start = item.get('start', '')[:10]
            if url and start:
                entries[url] = start
    return entries


def parse_detail(html, url, calendar_date):
    soup = BeautifulSoup(html, 'html.parser')
    event = soup.select_one('li[id^="concert"] .concert_post_wrapper')
    if event is None:
        return None

    title = clean_text(event.select_one('.concert_title')).replace('\n', ' ')
    venue = clean_text(event.select_one('.concert_place')).replace('\n', ' ')
    city = resolve_city(venue)
    # These intentionally unpublished booking placeholders contain no event
    # evidence, venue, or programme and are not concrete public listings.
    if title in {'公演あり', '非公開公演'} or not all((title, venue, city)):
        return None

    try:
        event_date = date.fromisoformat(calendar_date).isoformat()
    except ValueError:
        return None

    start_text = clean_text(event.select_one('.concert_start'))
    time_match = re.search(r'([0-2]?\d)\s*[:：]\s*([0-5]\d)\s*開演', start_text)
    time_from = None
    if time_match and int(time_match.group(1)) < 24:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    description_parts = []
    for selector in (
        '.concert_category', '.player_name', '.box2',
        '.concert_single_detail_wrapper .concert_detail > p',
    ):
        for node in event.select(selector):
            text = clean_text(node)
            if text and text not in description_parts:
                description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'JP',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_detail(url, calendar_date):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return parse_detail(response.text, url, calendar_date)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    entries = calendar_entries(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(fetch_detail, url, event_date): url
            for url, event_date in entries.items()
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Kansai Philharmonic concert detail',
                    event='crawler_detail_fetch_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class KansaiphilJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kansaiphil_jp',
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
    KansaiphilJpCrawler().run()


if __name__ == '__main__':
    main()
