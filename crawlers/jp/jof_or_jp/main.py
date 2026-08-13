import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.jof.or.jp/'
SOURCE = 'Japan Opera Foundation'
PERFORMANCES_API = f'{SOURCE_URL}wp-json/wp/v2/performance'

# The performance post type is the comprehensive first-party candidate feed.
# Its 主催イベント category mixes performances with courses, workshops, talks,
# and video, so the complete feed must go through potential-event classification.

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

CITY_HINTS = {
    '東京エレクトロンホール宮城': 'Sendai',
    '八戸市公会堂': 'Hachinohe', 'ベイシア文化ホール': 'Maebashi',
    'レクザムホール': 'Takamatsu', 'さぬき市': 'Sanuki',
    'テアトロ・ジーリオ・ショウワ': 'Kawasaki',
    '昭和音楽大学': 'Kawasaki', 'ユリホール': 'Kawasaki',
    'カワイ表参道': 'Tokyo', 'イイノホール': 'Tokyo',
    '和光大学ポプリホール鶴川': 'Tokyo',
    '帝国ホテル': 'Tokyo', '日生劇場': 'Tokyo',
    '第一生命ホール': 'Tokyo', '新国立劇場': 'Tokyo',
    '新宿': 'Tokyo', '世田谷': 'Tokyo', '千代田': 'Tokyo', '渋谷': 'Tokyo',
    '豊島': 'Tokyo', '中央区': 'Tokyo', '港区': 'Tokyo', '台東': 'Tokyo',
    '文京': 'Tokyo', '杉並': 'Tokyo', '目黒': 'Tokyo', '練馬': 'Tokyo',
    '東京': 'Tokyo', '横浜': 'Yokohama', '川崎': 'Kawasaki',
    '相模原': 'Sagamihara', '藤沢': 'Fujisawa', '鎌倉': 'Kamakura',
    '名古屋': 'Nagoya', '愛知': 'Nagoya', '高松': 'Takamatsu',
    '堺市': 'Sakai', '大阪': 'Osaka', '京都': 'Kyoto', '神戸': 'Kobe',
    '前橋': 'Maebashi', '高崎': 'Takasaki', '仙台': 'Sendai',
    '青森': 'Aomori', '札幌': 'Sapporo', '山中湖': 'Yamanakako',
    '船橋': 'Funabashi', '川越': 'Kawagoe', '福岡': 'Fukuoka',
    '広島': 'Hiroshima', '長崎': 'Nagasaki', '金沢': 'Kanazawa',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = html.unescape(str(value))
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response.json(), response.headers


def listing_items(session):
    items = []
    page = 1
    while True:
        payload, headers = get_json(
            session,
            PERFORMANCES_API,
            {'per_page': 100, 'page': page, 'orderby': 'date', 'order': 'desc'},
        )
        items.extend(payload)
        if page >= int(headers.get('X-WP-TotalPages', '1')):
            break
        page += 1

    return items


def resolve_city(venue, address):
    evidence = f'{venue}\n{address}'
    for hint, city in CITY_HINTS.items():
        if hint in evidence:
            return city

    # Japanese addresses normally state the municipality explicitly. Keeping
    # its first-party Japanese spelling is preferable to guessing a translation.
    match = re.search(r'(?:東京都|北海道|(?:京都|大阪)府|.{2,4}県)?([^\s、,〒]{1,12}(?:市|区|町|村))', address)
    if match:
        return match.group(1)

    return None


def parse_date(value):
    match = re.search(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日', value)
    if not match:
        return None
    try:
        return date(*map(int, match.groups())).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'([0-2]?\d)\s*[:：]\s*([0-5]\d)', value)
    if not match or int(match.group(1)) >= 24:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2)}'


def venue_details(soup):
    venue_node = soup.select_one(
        '#a-venue .p-special-access__title, '
        '#a-venue .p-performance-access__title, '
        '.p-special-access__title, .p-performance-access__title'
    )
    address_node = soup.select_one(
        '#a-venue .p-special-access__address, '
        '#a-venue .p-performance-access__address, '
        '.p-special-access__address, .p-performance-access__address'
    )

    if not venue_node:
        heading = soup.select_one('.p-performance-section__heading.is-venue')
        if heading:
            venue_node = heading.find_next_sibling(['p', 'div'])

    return clean_text(venue_node).replace('\n', ' '), clean_text(address_node)


def description_text(soup):
    main = soup.select_one('main')
    if not main:
        return None
    fragment = BeautifulSoup(str(main), 'html.parser')
    for node in fragment.select(
        'script, style, nav, form, .p-breadcrumb, .p-performance-ticket, '
        '.p-special-ticket, [class*="share"], [class*="sns"]'
    ):
        node.decompose()
    for heading in fragment.select('.is-ticket'):
        section = heading.find_parent('section')
        if section:
            section.decompose()
    text = clean_text(fragment)
    return text or None


def parse_detail(item, page_html, final_url):
    # Broken/retired custom posts sometimes redirect to the listing page.
    if final_url.rstrip('/') != item.get('link', '').rstrip('/'):
        return []

    soup = BeautifulSoup(page_html, 'html.parser')
    # Date ranges describe multi-day courses, exhibitions, cruises, or sales
    # windows rather than one defensible performance occurrence.
    if (item.get('acf') or {}).get('p_date_notation') == 'interval':
        return []
    title = clean_text((item.get('title') or {}).get('rendered')).replace('\n', ' ')
    venue, address = venue_details(soup)
    city = resolve_city(venue, address)
    if not title or not venue or not city:
        return []

    date_nodes = soup.select(
        '.p-performance-date__item, .p-special-home-date__item'
    )
    occurrences = []
    for node in date_nodes:
        text = clean_text(node)
        event_date = parse_date(text)
        if event_date:
            occurrences.append((event_date, parse_time(text)))

    # ACF is a structured fallback for simple pages whose visible layout lacks
    # the standard date component. It intentionally does not expand intervals.
    if not occurrences:
        acf = item.get('acf') or {}
        raw_date = str(acf.get('p_date') or '')
        if re.fullmatch(r'\d{8}', raw_date) and acf.get('p_date_notation') != 'interval':
            try:
                event_date = date(
                    int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:8])
                ).isoformat()
                occurrences.append((event_date, parse_time(str(acf.get('p_time') or ''))))
            except ValueError:
                pass

    description = description_text(soup)
    url = item.get('link') or ''
    records = []
    for event_date, time_from in dict.fromkeys(occurrences):
        records.append({
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
        })
    return records


def fetch_detail(session, item):
    response = session.get(item['link'], timeout=60)
    response.raise_for_status()
    return parse_detail(item, response.text, response.url)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = listing_items(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_detail, session, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape JOF performance detail',
                    event='crawler_detail_fetch_failed',
                    level='warning',
                    url=item.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(records, key=lambda record: (
        record['date'], record['time_from'] or '', record['title'], record['venue']
    ))


class JofOrJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jof_or_jp',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    JofOrJpCrawler().run()


if __name__ == '__main__':
    main()
