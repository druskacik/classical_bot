import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.shizukyo.or.jp/'
SOURCE = 'Mt. Fuji Shizuoka Symphony Orchestra'
FEEDS = (
    f'{SOURCE_URL}blog/categories/concerts',
    f'{SOURCE_URL}blog/categories/archives',
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

CITY_HINTS = {
    '静岡市': 'Shizuoka', '静岡': 'Shizuoka', '清水': 'Shizuoka',
    '浜松市': 'Hamamatsu', '浜松': 'Hamamatsu', '菊川市': 'Kikugawa',
    '菊川': 'Kikugawa', '富士市': 'Fuji', '富士宮市': 'Fujinomiya',
    '焼津市': 'Yaizu', '藤枝市': 'Fujieda', '島田市': 'Shimada',
    '掛川市': 'Kakegawa', '磐田市': 'Iwata', '沼津市': 'Numazu',
    '三島市': 'Mishima', '御殿場市': 'Gotemba', '裾野市': 'Susono',
    '伊東市': 'Ito', '伊豆市': 'Izu', '湖西市': 'Kosai',
    '牧之原市': 'Makinohara', '東京': 'Tokyo', '横浜': 'Yokohama',
    '名古屋': 'Nagoya', '大阪': 'Osaka', '甲府': 'Kofu',
}
VENUE_CITIES = {
    'アクトシティ浜松': 'Hamamatsu', 'サーラ音楽ホール': 'Hamamatsu',
    '静岡市民文化会館': 'Shizuoka', '静岡音楽館AOI': 'Shizuoka',
    'グランシップ': 'Shizuoka', 'マリナート': 'Shizuoka',
    '由比生涯学習交流館': 'Shizuoka', '駿府の工房 匠宿': 'Shizuoka',
    '菊川文化会館アエル': 'Kikugawa', '東京オペラシティ': 'Tokyo',
    'すみだトリフォニーホール': 'Tokyo', 'サントリーホール': 'Tokyo',
}
VENUE_WORDS = re.compile(
    r'(ホール|会館|劇場|音楽館|センター|グランシップ|マリナート|アエル|匠宿|寺|神社)'
)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def listing_urls(session, feed_url):
    first = session.get(feed_url, timeout=60)
    first.raise_for_status()
    soup = BeautifulSoup(first.text, 'html.parser')
    pages = [1]
    for link in soup.select('a[href*="/page/"]'):
        match = re.search(r'/page/(\d+)', link.get('href', ''))
        if match:
            pages.append(int(match.group(1)))

    urls = set()
    for page_number in range(1, max(pages) + 1):
        page_soup = soup
        if page_number > 1:
            response = session.get(f'{feed_url}/page/{page_number}', timeout=60)
            response.raise_for_status()
            page_soup = BeautifulSoup(response.text, 'html.parser')
        for item in page_soup.select('[data-hook="post-list-item"]'):
            link = item.select_one('a[href*="/post/"]')
            if link:
                urls.add(urljoin(SOURCE_URL, link.get('href')))
    return urls


def title_dates(title):
    match = re.search(r'(20\d{2})[/.年](\d{1,2})[/.月](\d{1,2})', title)
    if not match:
        return []
    year, month, day = map(int, match.groups())
    values = [(year, month, day)]
    tail = title[match.end():]
    for next_month, next_day in re.findall(r'(?:[・･,，]|・?)(?:(\d{1,2})[/.月])?(\d{1,2})(?:日|\()', tail):
        candidate = (year, int(next_month) if next_month else month, int(next_day))
        if candidate not in values:
            values.append(candidate)
    result = []
    for parts in values:
        try:
            result.append(date(*parts))
        except ValueError:
            continue
    return result


def resolve_city(text):
    for hint, city in VENUE_CITIES.items():
        if hint in text:
            return city
    for hint, city in CITY_HINTS.items():
        if hint in text:
            return city
    return None


def occurrence_block(lines, event_date):
    patterns = (
        f'{event_date.year}年{event_date.month}月{event_date.day}日',
        f'{event_date.year}/{event_date.month}/{event_date.day}',
        f'{event_date.month}月{event_date.day}日',
    )
    indexes = [i for i, line in enumerate(lines) if any(value in line for value in patterns)]
    if not indexes:
        return []
    # Prefer a dated line that also contains performance timing; publication,
    # sales and application dates normally do not contain 開演/開場.
    index = next((i for i in indexes if re.search(r'開演|開場|\d{1,2}:\d{2}', lines[i])), indexes[-1])
    return lines[max(0, index - 2):index + 12]


def extract_venue(block):
    candidates = []
    for line in block:
        value = re.sub(r'^[●■◆◇]?\s*(?:会場|場所)\s*[:：]?\s*', '', line).strip()
        if not value or not VENUE_WORDS.search(value):
            continue
        if re.search(
            r'(チケット|プレイガイド|問い合わせ|駐車場|会場地図|劇場・音楽堂等|'
            r'この公演|ございません|ご注意)', value
        ):
            continue
        candidates.append(value)
    if not candidates:
        return None
    # Prefer the first named venue after the occurrence date. A bare room name
    # can precede a full venue name on some older posts.
    return next(
        (value for value in candidates if value not in {'大ホール', '中ホール', '小ホール'}),
        candidates[0],
    )


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article')
    if not article:
        return []
    heading = article.select_one('h1')
    title = clean_text(heading).replace('\n', ' ')
    dates = title_dates(title)
    if not title or not dates:
        return []

    for node in article.select('script, style, nav, form, footer, [data-hook="post-footer"]'):
        node.decompose()
    description = clean_text(article)
    lines = [line.strip() for line in description.splitlines() if line.strip()]
    records = []
    for event_date in dates:
        block = occurrence_block(lines, event_date)
        venue = extract_venue(block)
        context = '\n'.join(block)
        city = resolve_city(venue or '') or resolve_city(context)
        if not venue or not city:
            continue
        time_match = re.search(r'(?<!開場)(\d{1,2})[:：](\d{2})\s*開演', context)
        if not time_match:
            time_match = re.search(r'(\d{1,2})[:：](\d{2})\s*[～〜-]', context)
        time_from = None
        if time_match and int(time_match.group(1)) < 24:
            time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
        records.append({
            'title': title,
            'date': event_date.isoformat(),
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'JP',
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def fetch_detail(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return parse_detail(response.text, url)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = set()
    for feed in FEEDS:
        urls.update(listing_urls(session, feed))

    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch_detail, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Shizukyo concert detail',
                    event='crawler_detail_fetch_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class ShizukyoOrJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='shizukyo_or_jp',
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
    ShizukyoOrJpCrawler().run()


if __name__ == '__main__':
    main()
