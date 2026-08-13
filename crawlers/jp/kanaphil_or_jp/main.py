import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.kanaphil.or.jp/'
SOURCE = 'Kanagawa Philharmonic Orchestra'
CONCERT_URL = f'{SOURCE_URL}concert/'
ARCHIVE_URL = f'{SOURCE_URL}concert_archives/y{{year}}/'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

# Detail pages only publish a hall name. These first-party events tour, so a
# home-city fallback would be unsafe; known halls and explicit municipality
# text are resolved instead and unknown locations are skipped.
VENUE_CITIES = {
    '横浜みなとみらいホール': 'Yokohama', '神奈川県民ホール': 'Yokohama',
    '神奈川県立音楽堂': 'Yokohama', '横浜美術館': 'Yokohama',
    '関内ホール': 'Yokohama', 'かなっくホール': 'Yokohama',
    'フィリアホール': 'Yokohama', '戸塚区民文化センター': 'Yokohama',
    'ミューザ川崎': 'Kawasaki', 'カルッツかわさき': 'Kawasaki',
    '川崎市スポーツ・文化総合センター': 'Kawasaki',
    '鎌倉芸術館': 'Kamakura', '藤沢市民会館': 'Fujisawa',
    '茅ヶ崎市民文化会館': 'Chigasaki', 'ひらしん平塚文化芸術ホール': 'Hiratsuka',
    '相模女子大学グリーンホール': 'Sagamihara', '杜のホールはしもと': 'Sagamihara',
    '横須賀芸術劇場': 'Yokosuka', 'よこすか芸術劇場': 'Yokosuka',
    '海老名市文化会館': 'Ebina', '綾瀬市オーエンス文化会館': 'Ayase',
    '大和市文化創造拠点シリウス': 'Yamato', 'ハーモニーホール座間': 'Zama',
    '小田原三の丸ホール': 'Odawara', '小田原市民会館': 'Odawara',
    '南足柄市文化会館': 'Minamiashigara', '伊勢原市民文化会館': 'Isehara',
    '厚木市文化会館': 'Atsugi', '秦野市文化会館': 'Hadano',
    '町田市民ホール': 'Machida', 'グランシップ': 'Shizuoka',
    '東京文化会館': 'Tokyo', '東京芸術劇場': 'Tokyo',
    'サントリーホール': 'Tokyo', '東京国際フォーラム': 'Tokyo',
}
CITY_HINTS = {
    '横浜': 'Yokohama', '川崎': 'Kawasaki', '鎌倉': 'Kamakura',
    '藤沢': 'Fujisawa', '茅ヶ崎': 'Chigasaki', '平塚': 'Hiratsuka',
    '相模原': 'Sagamihara', '横須賀': 'Yokosuka', '三浦': 'Miura',
    '海老名': 'Ebina', '綾瀬': 'Ayase', '大和': 'Yamato', '座間': 'Zama',
    '小田原': 'Odawara', '南足柄': 'Minamiashigara', '伊勢原': 'Isehara',
    '厚木': 'Atsugi', '秦野': 'Hadano', '町田': 'Machida',
    '静岡': 'Shizuoka', '東京': 'Tokyo',
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
    for name, city in VENUE_CITIES.items():
        if name in venue:
            return city
    for hint, city in CITY_HINTS.items():
        if hint in venue:
            return city
    return None


def parse_date(text):
    match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', text)
    if not match:
        return None
    try:
        return date(*map(int, match.groups())).isoformat()
    except ValueError:
        return None


def parse_detail(html, url):
    # Every detail page repeats the full multi-year calendar after the useful
    # article. Excluding it keeps catalogue-wide scraping memory bounded.
    article_html = html.split('<div class="concert-cal', 1)[0]
    soup = BeautifulSoup(article_html, 'html.parser')
    title = clean_text(soup.select_one('.concert__ttl')).replace('\n', ' ')
    event_date = parse_date(clean_text(soup.select_one('.concert__date')))
    venue = clean_text(soup.select_one('.concert-place')).replace('\n', ' ')
    city = resolve_city(venue)
    if not all((title, event_date, venue, city)):
        return None

    time_match = re.search(r'(?<!\d)([0-2]?\d):([0-5]\d)', clean_text(soup.select_one('.concert-open')))
    time_from = None
    if time_match and int(time_match.group(1)) < 24:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    description_nodes = soup.select('.concert-block-top, .concert-programs')
    description = '\n\n'.join(filter(None, (clean_text(node) for node in description_nodes)))
    return {
        'title': title, 'date': event_date, 'url': url, 'time_from': time_from,
        'venue': venue, 'city': city, 'country_code': 'JP',
        'description': description or None, 'source_url': SOURCE_URL, 'source': SOURCE,
    }


def parse_archive(html, url, year):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for row in soup.select('table.concert-archive-page-list tr'):
        head = row.select_one('th')
        body = row.select_one('td')
        parts = head.find_all('p', recursive=False) if head else []
        if len(parts) < 2:
            continue
        raw_date = clean_text(parts[0])
        venue = clean_text(parts[1]).replace('\n', ' ')
        match = re.fullmatch(r'(\d{2})\.(\d{2})\.(\d{2})', raw_date)
        city = resolve_city(venue)
        if not match or not venue or not city:
            continue
        try:
            event_date = date(year, int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            continue
        description = '\n\n'.join(filter(None, (clean_text(head), clean_text(body))))
        records.append({
            'title': f'{SOURCE} concert at {venue}', 'date': event_date, 'url': url,
            'time_from': None, 'venue': venue, 'city': city, 'country_code': 'JP',
            'description': description or None, 'source_url': SOURCE_URL, 'source': SOURCE,
        })
    return records


def fetch_detail(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return parse_detail(response.text, url)


def fetch_archive(year):
    url = ARCHIVE_URL.format(year=year)
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return parse_archive(response.text, url, year)


def get_concerts():
    response = requests.get(CONCERT_URL, headers=HEADERS, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    urls = sorted({
        node.get('data-href') for node in soup.select('[data-href*="/concert/"]')
        if re.fullmatch(r'https://www\.kanaphil\.or\.jp/concert/\d+/', node.get('data-href', ''))
    })

    records = []
    jobs = [('detail', url) for url in urls]
    jobs.extend(('archive', year) for year in range(1970, date.today().year))
    # Pages contain a large embedded calendar; keep concurrency modest so the
    # temporary BeautifulSoup trees do not create excessive peak memory use.
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(fetch_detail, value) if kind == 'detail'
            else executor.submit(fetch_archive, value): (kind, value)
            for kind, value in jobs
        }
        for future in as_completed(futures):
            kind, value = futures[future]
            try:
                result = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Kanagawa Philharmonic page',
                    event='crawler_page_fetch_failed', level='warning',
                    url=value if kind == 'detail' else ARCHIVE_URL.format(year=value),
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            if kind == 'detail':
                if result:
                    records.append(result)
            else:
                records.extend(result)
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class KanaphilOrJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kanaphil_or_jp', source=SOURCE, source_url=SOURCE_URL,
        country_code='JP', upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        return get_concerts()


def main():
    KanaphilOrJpCrawler().run()


if __name__ == '__main__':
    main()
