import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://nikikai.jp/'
SOURCE = 'Tokyo Nikikai Opera Foundation'
INDEX_URLS = (
    f'{SOURCE_URL}lineup/',
    f'{SOURCE_URL}lineup/past/',
    f'{SOURCE_URL}concert/',
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

# Detail pages generally give a hall and nearby station rather than an address.
# These are recurring first-party venues in the opera and concert archives.
VENUE_CITIES = {
    '新国立劇場': 'Tokyo', '東京文化会館': 'Tokyo', '東京芸術劇場': 'Tokyo',
    '日生劇場': 'Tokyo', 'サントリーホール': 'Tokyo', '紀尾井ホール': 'Tokyo',
    'オーチャードホール': 'Tokyo', 'Bunkamura': 'Tokyo',
    '渋谷区文化総合センター': 'Tokyo', 'カワイ表参道': 'Tokyo',
    '北とぴあ': 'Tokyo', '第一生命ホール': 'Tokyo', '王子ホール': 'Tokyo',
    '東京オペラシティ': 'Tokyo', 'すみだトリフォニーホール': 'Tokyo',
    '杉並公会堂': 'Tokyo', '浜離宮朝日ホール': 'Tokyo', 'めぐろパーシモン': 'Tokyo',
    '東京国際フォーラム': 'Tokyo', '武蔵野市民文化会館': 'Musashino',
    'フェニーチェ堺': 'Sakai', '札幌文化芸術劇場': 'Sapporo',
    'やまぎん県民ホール': 'Yamagata', 'よこすか芸術劇場': 'Yokosuka',
    '神奈川県民ホール': 'Yokohama', '横浜みなとみらいホール': 'Yokohama',
    'ミューザ川崎': 'Kawasaki', 'テアトロ・ジーリオ・ショウワ': 'Kawasaki',
    '愛知県芸術劇場': 'Nagoya', '兵庫県立芸術文化センター': 'Nishinomiya',
    '京都コンサートホール': 'Kyoto', 'びわ湖ホール': 'Otsu',
    '富山市芸術文化ホール': 'Toyama', 'オーバード・ホール': 'Toyama',
    'とりぎん文化会館': 'Tottori', 'iichiko総合文化センター': 'Oita',
    '大分県立総合文化センター': 'Oita', '熊本県立劇場': 'Kumamoto',
}


def clean_text(node):
    if not node:
        return ''
    text = node.get_text('\n', strip=True) if hasattr(node, 'get_text') else str(node)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def listing_urls(session):
    urls = set()
    for index_url in INDEX_URLS:
        response = session.get(index_url, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        for link in soup.select('a[href]'):
            url = canonical_url(urljoin(index_url, link['href']))
            path = urlsplit(url).path
            is_detail = (
                (path.startswith('/lineup/') and path not in ('/lineup/', '/lineup/past/'))
                or (path.startswith('/concert/') and path != '/concert/')
            )
            if not is_detail or '/program/' in path or path.endswith('/index.html'):
                continue
            # This is an archive-range overview, not a concrete performance.
            if path.endswith('/1952to2000.html'):
                continue
            urls.add(url)
    return sorted(urls)


def resolve_city(venue, place_text):
    combined = f'{venue}\n{place_text}'
    for hint, city in VENUE_CITIES.items():
        if hint in combined:
            return city

    # Prefer an explicitly printed Japanese municipality when available.
    match = re.search(
        r'(?:東京都|北海道|(?:京都|大阪)府|.{2,3}県)?'
        r'([一-龥ぁ-んァ-ヶ]{2,12}(?:市|区|町|村))',
        combined,
    )
    if match:
        return match.group(1)
    return None


def parse_occurrences(date_text):
    normalized = re.sub(r'\s+', ' ', date_text)
    date_matches = list(re.finditer(
        r'((?:19|20)\d{2})\s*[.年/]\s*(\d{1,2})\s*[.月/]\s*(\d{1,2})(?:\s*日)?',
        normalized,
    ))
    occurrences = []
    for index, match in enumerate(date_matches):
        try:
            event_date = date(*(int(value) for value in match.groups())).isoformat()
        except ValueError:
            continue
        end = date_matches[index + 1].start() if index + 1 < len(date_matches) else len(normalized)
        segment = normalized[match.end():end]
        time_match = re.search(r'([01]?\d|2[0-3]):([0-5]\d)\s*開演', segment)
        time_from = None
        if time_match:
            time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
        occurrences.append((event_date, time_from))
    return occurrences


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    area = soup.select_one('.operaDetailArea')
    date_node = soup.select_one('.operaLineupDate')
    place_node = soup.select_one('.operaLineupPlace')
    if not all((area, date_node, place_node)):
        return []

    title_node = area.select_one('.operaLineupRight h3, h3')
    if not title_node:
        title_node = soup.select_one('title')
    title = clean_text(title_node).replace('\n', ' ')
    if title_node and title_node.name == 'title':
        title = re.split(r'[｜|]', title, maxsplit=1)[0].strip()

    place_lines = [line for line in clean_text(place_node).splitlines() if line]
    venue = ''
    for line in place_lines:
        if line in ('会場名', '会場') or line.startswith(('【アクセス', 'アクセス')):
            continue
        venue = line
        break
    city = resolve_city(venue, clean_text(place_node)) if venue else None
    occurrences = parse_occurrences(clean_text(date_node))
    if not all((title, venue, city, occurrences)):
        return []

    description_nodes = area.select('.detailBody')
    if not description_nodes:
        description_nodes = area.select('.tabContents')
    description = '\n\n'.join(filter(None, (clean_text(node) for node in description_nodes)))
    if not description:
        description = clean_text(area)

    return [{
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
    } for event_date, time_from in occurrences]


def fetch_detail(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return parse_detail(response.content, url)


class NikikaiJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nikikai_jp',
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
        urls = listing_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(fetch_detail, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape Tokyo Nikikai event detail',
                        event='crawler_detail_fetch_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    NikikaiJpCrawler().run()


if __name__ == '__main__':
    main()
