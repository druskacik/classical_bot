import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ac-orchestra.com/'
SOURCE = 'Aichi Chamber Orchestra'
POSTS_API = f'{SOURCE_URL}wp-json/wp/v2/posts'
# These first-party categories are the complete current and historical concert
# feeds: 開催前 (upcoming) and 終了 (completed).
CONCERT_CATEGORY_IDS = (23, 16)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

# Explicit touring locations take precedence over home-region defaults. Japanese
# venue names normally include their municipality; the hall table covers common
# exceptions whose names omit it.
CITY_HINTS = {
    '四日市': 'Yokkaichi', '名古屋': 'Nagoya', '豊橋': 'Toyohashi',
    '岡崎': 'Okazaki', '一宮': 'Ichinomiya', '瀬戸': 'Seto', '半田': 'Handa',
    '春日井': 'Kasugai', '豊川': 'Toyokawa', '刈谷': 'Kariya', '豊田': 'Toyota',
    '安城': 'Anjo', '西尾': 'Nishio', '蒲郡': 'Gamagori', '犬山': 'Inuyama',
    '常滑': 'Tokoname', '江南': 'Konan', '小牧': 'Komaki', '稲沢': 'Inazawa',
    '東海市': 'Tokai', '大府': 'Obu', '知多市': 'Chita', '知立': 'Chiryu',
    '尾張旭': 'Owariasahi', '高浜': 'Takahama', '岩倉': 'Iwakura',
    '豊明': 'Toyoake', '日進': 'Nisshin', '田原': 'Tahara', '愛西': 'Aisai',
    '清須': 'Kiyosu', '北名古屋': 'Kitanagoya', '弥富': 'Yatomi',
    'みよし': 'Miyoshi', 'あま市': 'Ama', '長久手': 'Nagakute',
    '東郷町': 'Togo', '豊山町': 'Toyoyama', '大口町': 'Oguchi',
    '扶桑町': 'Fuso', '大治町': 'Oharu', '蟹江町': 'Kanie',
    '阿久比町': 'Agui', '東浦町': 'Higashiura', '南知多町': 'Minamichita',
    '美浜町': 'Mihama', '武豊町': 'Taketoyo', '幸田町': 'Kota',
    '設楽町': 'Shitara', '東栄町': 'Toei', '飛島村': 'Tobishima',
    '下呂': 'Gero', '岐阜': 'Gifu', '多治見': 'Tajimi', '可児': 'Kani',
    '浜松': 'Hamamatsu', '静岡': 'Shizuoka', '津市': 'Tsu', '鈴鹿': 'Suzuka',
    '大阪': 'Osaka', '京都': 'Kyoto', '神戸': 'Kobe', '東京': 'Tokyo',
    '横浜': 'Yokohama', '川崎': 'Kawasaki', '長野': 'Nagano',
    '松本': 'Matsumoto', '金沢': 'Kanazawa', '福井': 'Fukui',
}

HALL_CITIES = {
    '愛知県芸術劇場': 'Nagoya', '愛知芸術文化センター': 'Nagoya',
    'しらかわホール': 'Nagoya', '宗次ホール': 'Nagoya',
    '電気文化会館': 'Nagoya', '三井住友海上しらかわホール': 'Nagoya',
    '日本特殊陶業市民会館': 'Nagoya', 'Niterra日本特殊陶業市民会館': 'Nagoya',
    'アートピアホール': 'Nagoya', '熱田文化小劇場': 'Nagoya',
    '東文化小劇場': 'Nagoya', '瑞穂文化小劇場': 'Nagoya',
    '昭和文化小劇場': 'Nagoya', '中川文化小劇場': 'Nagoya',
    '守山文化小劇場': 'Nagoya', '緑文化小劇場': 'Nagoya',
    '名東文化小劇場': 'Nagoya', '港文化小劇場': 'Nagoya',
    'ウィルあいち': 'Nagoya', '中電ホール': 'Nagoya',
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


def listing_posts(session):
    posts = {}
    for category_id in CONCERT_CATEGORY_IDS:
        page = 1
        while True:
            response = session.get(
                POSTS_API,
                params={
                    'categories': category_id, 'per_page': 100, 'page': page,
                    '_fields': 'id,link',
                },
                timeout=60,
            )
            response.raise_for_status()
            for post in response.json():
                if post.get('id') and post.get('link'):
                    posts[post['id']] = post['link']
            if page >= int(response.headers.get('X-WP-TotalPages', '1')):
                break
            page += 1
    return list(posts.values())


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.type-post')
    if not article:
        return None

    title_node = article.select_one('.entry-title')
    date_node = article.select_one('.konsa_jikan')
    time_node = article.select_one('.konsa_tokei')
    venue_node = article.select_one('.konsa_basyo')
    title = clean_text(title_node).replace('\n', ' ')
    raw_date = clean_text(date_node)
    venue = clean_text(venue_node).replace('\n', ' ')
    city = resolve_city(venue)
    date_match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', raw_date)
    if not all((title, date_match, venue, city)):
        return None
    try:
        event_date = date(*map(int, date_match.groups())).isoformat()
    except ValueError:
        return None

    time_match = re.search(r'開演\s*[:：]\s*([0-2]?\d)\s*[:：]\s*([0-5]\d)', clean_text(time_node))
    time_from = None
    if time_match and int(time_match.group(1)) < 24:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    # Ticketing and site chrome follow this node. Removing them retains the
    # event introduction, performers, listening notes, and complete programme.
    for node in article.select(
        '.tikejyou, .ticket, .con_menu, script, style, nav, form, .post-navigation'
    ):
        node.decompose()
    description = clean_text(article)

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'JP',
        'description': description or None,
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
    urls = listing_posts(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch_detail, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Aichi Chamber Orchestra concert detail',
                    event='crawler_detail_fetch_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class AcOrchestraComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ac_orchestra_com',
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
    AcOrchestraComCrawler().run()


if __name__ == '__main__':
    main()
