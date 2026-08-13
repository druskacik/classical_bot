import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.osaka-phil.com/'
SOURCE = 'Osaka Philharmonic Orchestra'

# These are the complete first-party performance categories shown by the site.
# Category archives retain the category in their stable /page/N/ pagination.
CATEGORY_SLUGS = (
    'subscription',
    'special-performance',
    'masterpiece-concert',
    'independent-performance',
    'tour',
    'request-performance',
    'other',
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

# Touring venues must not inherit Osaka. Prefer explicit municipality text,
# followed by well-known halls whose names do not contain their city.
CITY_HINTS = {
    '大阪': 'Osaka', '堺市': 'Sakai', '豊中': 'Toyonaka', '吹田': 'Suita',
    '枚方': 'Hirakata', '東大阪': 'Higashiosaka', '八尾': 'Yao',
    '京都': 'Kyoto', '神戸': 'Kobe', '西宮': 'Nishinomiya', '尼崎': 'Amagasaki',
    '奈良': 'Nara', '橿原': 'Kashihara', '大津': 'Otsu', '和歌山': 'Wakayama',
    '福岡': 'Fukuoka', '北九州': 'Kitakyushu', '東京': 'Tokyo', '横浜': 'Yokohama',
    '名古屋': 'Nagoya', '広島': 'Hiroshima', '岡山': 'Okayama', '高松': 'Takamatsu',
    '徳島': 'Tokushima', '松山': 'Matsuyama', '金沢': 'Kanazawa', '長野': 'Nagano',
}

HALL_CITIES = {
    'フェスティバルホール': 'Osaka',
    'ザ・シンフォニーホール': 'Osaka',
    '大阪フィルハーモニー会館': 'Osaka',
    '住友生命いずみホール': 'Osaka',
    'いずみホール': 'Osaka',
    'NHK大阪ホール': 'Osaka',
    'フェニーチェ堺': 'Sakai',
    'ロームシアター京都': 'Kyoto',
    '兵庫県立芸術文化センター': 'Nishinomiya',
    '福岡シンフォニーホール': 'Fukuoka',
    'アクロス福岡': 'Fukuoka',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u2002', ' ')
    text = text.replace('\u200b', '')
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


def get_html(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response.text


def listing_urls(session):
    urls = set()
    for category in CATEGORY_SLUGS:
        page = 1
        while True:
            base = f'{SOURCE_URL}events/category/{category}/'
            url = base if page == 1 else urljoin(base, f'page/{page}/')
            soup = BeautifulSoup(get_html(session, url), 'html.parser')
            page_urls = {
                urljoin(SOURCE_URL, link['href'])
                for link in soup.select('a.event-list__link[href]')
            }
            new_urls = page_urls - urls
            urls.update(page_urls)
            next_url = urljoin(base, f'page/{page + 1}/')
            has_next = any(
                urljoin(url, link.get('href', '')) == next_url
                for link in soup.select('a[href]')
            )
            if not has_next or not new_urls:
                break
            page += 1
    return sorted(urls)


def detail_fields(soup):
    fields = {}
    for item in soup.select('.event-single__list-item'):
        term = item.select_one('.event-single__term')
        value = item.select_one('.event-single__desc')
        if term and value:
            fields[clean_text(term)] = clean_text(value)
    return fields


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('.event-single__title-text')).replace('\n', ' ')
    fields = detail_fields(soup)
    raw_date = fields.get('開催日時', '')
    venue = re.sub(r'\s*[（(]\s*アクセス[^）)]*[）)]\s*$', '', fields.get('会場', '')).strip()
    city = resolve_city(venue)
    date_match = re.search(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日', raw_date)
    if not title or not date_match or not venue or not city:
        return None
    if venue.lower().startswith('closed') or '関係者のみ' in venue:
        return None
    try:
        event_date = date(*map(int, date_match.groups())).isoformat()
    except ValueError:
        return None

    raw_time = fields.get('開演時間', '')
    time_match = re.search(r'(?<!\d)([0-2]?\d)\s*[:：]\s*([0-5]\d)', raw_time)
    time_from = None
    if time_match and int(time_match.group(1)) < 24:
        candidate = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
        if candidate != '00:00':
            time_from = candidate

    description_parts = []
    for label in ('出演者', '曲目', '備考'):
        value = fields.get(label)
        if value:
            description_parts.append(f'{label}\n{value}')
    body = clean_text(soup.select_one('.event-single__content'))
    if body:
        description_parts.append(body)

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'JP',
        'description': clean_text('\n\n'.join(description_parts)) or None,
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
    # Detail pages are unusually large; keep concurrency deliberately low to
    # avoid excessive memory use while BeautifulSoup builds several trees.
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(fetch_detail, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Osaka Philharmonic concert detail',
                    event='crawler_detail_fetch_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class OsakaPhilComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='osaka_phil_com',
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
    OsakaPhilComCrawler().run()


if __name__ == '__main__':
    main()
