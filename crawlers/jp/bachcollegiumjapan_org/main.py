import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://bachcollegiumjapan.org/'
SITEMAP_URL = f'{SOURCE_URL}wp-sitemap-posts-schedule-1.xml'
SOURCE = 'Bach Collegium Japan'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

# The schedule includes tours. These hints deliberately take precedence over
# the ensemble's Tokyo home base and cover the locations used in its archive.
CITY_HINTS = {
    '東京': ('Tokyo', 'JP'), '調布': ('Chofu', 'JP'), '神戸': ('Kobe', 'JP'),
    '名古屋': ('Nagoya', 'JP'), '横浜': ('Yokohama', 'JP'), '川崎': ('Kawasaki', 'JP'),
    '大阪': ('Osaka', 'JP'), '京都': ('Kyoto', 'JP'), '札幌': ('Sapporo', 'JP'),
    '福岡': ('Fukuoka', 'JP'), '北九州': ('Kitakyushu', 'JP'), '広島': ('Hiroshima', 'JP'),
    '東広島': ('Higashihiroshima', 'JP'), '長崎': ('Nagasaki', 'JP'),
    '佐世保': ('Sasebo', 'JP'), '大分': ('Oita', 'JP'), '鹿児島': ('Kagoshima', 'JP'),
    '高岡': ('Takaoka', 'JP'), '金沢': ('Kanazawa', 'JP'), '高崎': ('Takasaki', 'JP'),
    '松本': ('Matsumoto', 'JP'), '静岡': ('Shizuoka', 'JP'), '浜松': ('Hamamatsu', 'JP'),
    '豊田': ('Toyota', 'JP'), '岡崎': ('Okazaki', 'JP'), '所沢': ('Tokorozawa', 'JP'),
    'さいたま': ('Saitama', 'JP'), '水戸': ('Mito', 'JP'), '盛岡': ('Morioka', 'JP'),
    '軽井沢': ('Karuizawa', 'JP'), '八ヶ岳': ('Minamimaki', 'JP'),
    '青山学院': ('Tokyo', 'JP'), 'サントリーホール': ('Tokyo', 'JP'),
    'よみうり大手町': ('Tokyo', 'JP'), 'めぐろパーシモン': ('Tokyo', 'JP'),
    'ヒルサイドプラザ': ('Tokyo', 'JP'), 'しらかわホール': ('Nagoya', 'JP'),
    '神奈川県立音楽堂': ('Yokohama', 'JP'), '紀尾井ホール': ('Tokyo', 'JP'),
    '東京芸術劇場': ('Tokyo', 'JP'), '東京国際フォーラム': ('Tokyo', 'JP'),
    'オーチャードホール': ('Tokyo', 'JP'), '浜離宮朝日ホール': ('Tokyo', 'JP'),
    'Amsterdam': ('Amsterdam', 'NL'), 'アムステルダム': ('Amsterdam', 'NL'),
    'Leipzig': ('Leipzig', 'DE'), 'ライプツィヒ': ('Leipzig', 'DE'),
    'Berlin': ('Berlin', 'DE'), 'ベルリン': ('Berlin', 'DE'),
    'Hamburg': ('Hamburg', 'DE'), 'ハンブルク': ('Hamburg', 'DE'),
    'Cologne': ('Cologne', 'DE'), 'ケルン': ('Cologne', 'DE'),
    'Düsseldorf': ('Dusseldorf', 'DE'), 'デュッセルドルフ': ('Dusseldorf', 'DE'),
    'Arnstadt': ('Arnstadt', 'DE'), 'Eisenach': ('Eisenach', 'DE'),
    'Paris': ('Paris', 'FR'), 'パリ': ('Paris', 'FR'), 'Toulouse': ('Toulouse', 'FR'),
    'London': ('London', 'GB'), 'ロンドン': ('London', 'GB'),
    'Madrid': ('Madrid', 'ES'), 'マドリード': ('Madrid', 'ES'),
    'Dublin': ('Dublin', 'IE'), 'ダブリン': ('Dublin', 'IE'),
    'Vienna': ('Vienna', 'AT'), 'ウィーン': ('Vienna', 'AT'),
    'Warsaw': ('Warsaw', 'PL'), 'ワルシャワ': ('Warsaw', 'PL'),
    'Wrocław': ('Wroclaw', 'PL'), 'ヴロツワフ': ('Wroclaw', 'PL'),
    'Katowice': ('Katowice', 'PL'), 'カトヴィツェ': ('Katowice', 'PL'),
    'Antwerp': ('Antwerp', 'BE'), 'アントワープ': ('Antwerp', 'BE'),
    'The Hague': ('The Hague', 'NL'), 'ハーグ': ('The Hague', 'NL'),
    'Lausanne': ('Lausanne', 'CH'), 'ローザンヌ': ('Lausanne', 'CH'),
    'Fribourg': ('Fribourg', 'CH'), 'Groningen': ('Groningen', 'NL'),
    'Varaždin': ('Varazdin', 'HR'), 'New York': ('New York', 'US'),
    'ニューヨーク': ('New York', 'US'), 'Shanghai': ('Shanghai', 'CN'),
    '上海': ('Shanghai', 'CN'), 'Melbourne': ('Melbourne', 'AU'),
    'Sydney': ('Sydney', 'AU'), 'Auckland': ('Auckland', 'NZ'),
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def schedule_urls(session):
    response = session.get(SITEMAP_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'xml')
    return [node.get_text(strip=True) for node in soup.select('url > loc')]


def resolve_place(venue, body):
    evidence = f'{venue}\n{body}'
    for hint, place in CITY_HINTS.items():
        if hint.casefold() in evidence.casefold():
            return place
    return None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.schedule')
    content = article.select_one('.entry-content-page') if article else None
    title_node = article.select_one('.article-header .title h1') if article else None
    body = clean_text(content)
    title = clean_text(title_node).replace('\n', ' ')
    if not title and body:
        title = body.splitlines()[0]
    if not title or not body:
        return None

    date_match = re.search(r'(20\d{2})\s*年\s*(\d{1,2})[.月]\s*(\d{1,2})', body)
    if not date_match:
        date_match = re.search(r'(20\d{2})[./-](\d{1,2})[./-](\d{1,2})', body)
    if not date_match:
        return None
    try:
        event_date = date(*map(int, date_match.groups())).isoformat()
    except ValueError:
        return None

    time_match = re.search(r'(?<!\d)([0-2]?\d)\s*[:：]\s*([0-5]\d)', body[date_match.end():])
    time_from = None
    if time_match and int(time_match.group(1)) < 24:
        hour = int(time_match.group(1))
        time_context = body[max(date_match.end(), time_match.start() - 8):time_match.end() + 3]
        if ('午後' in time_context or re.search(r'PM', time_context, re.I)) and hour < 12:
            hour += 12
        if ('午前' in time_context or re.search(r'AM', time_context, re.I)) and hour == 12:
            hour = 0
        time_from = f'{hour:02d}:{time_match.group(2)}'

    # On schedule pages the venue is the first non-empty line after the dated
    # occurrence, following any weekday/opening-time text on the same block.
    tail = body[date_match.end():]
    lines = [line.strip(' ~〜｜|') for line in tail.splitlines() if line.strip(' ~〜｜|')]
    venue = ''
    for line in lines:
        if re.fullmatch(r'[（(]?[月火水木金土日祝・曜]+[）)]?(?:\s*\d{1,2}[:：]\d{2}.*)?', line):
            continue
        line = re.sub(r'^[（(][^）)]{1,8}[）)]\s*', '', line)
        line = re.sub(r'^\d{1,2}[:：]\d{2}(?:\s*(?:開演|~|〜|-))?\s*', '', line)
        if not line or re.match(r'^(開場|開演|Start|Open)\b', line, re.I):
            continue
        venue = line.strip()
        break
    if not venue or len(venue) > 180:
        return None

    place = resolve_place(venue, body[:body.find(venue) + len(venue)])
    if not place:
        return None
    city, country_code = place

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': body or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_detail(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return parse_detail(response.text, url)


class BachcollegiumjapanOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bachcollegiumjapan_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = schedule_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(fetch_detail, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Bach Collegium Japan concert detail',
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
    BachcollegiumjapanOrgCrawler().run()


if __name__ == '__main__':
    main()
