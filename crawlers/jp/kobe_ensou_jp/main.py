import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.kobe-ensou.jp/'
SOURCE = 'Kobe City Chamber Orchestra & Kobe City Philharmonic Chorus'
SCHEDULE_API = f'{SOURCE_URL}wp-json/wp/v2/schedule'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

# The ensembles perform outside their home city, so only explicit municipality
# text and known halls are used. Unknown locations are skipped rather than
# silently assigned to Kobe.
CITY_HINTS = {
    '神戸': 'Kobe', '芦屋': 'Ashiya', '西宮': 'Nishinomiya',
    '尼崎': 'Amagasaki', '伊丹': 'Itami', '宝塚': 'Takarazuka',
    '川西': 'Kawanishi', '三田': 'Sanda', '明石': 'Akashi',
    '加古川': 'Kakogawa', '姫路': 'Himeji', '豊岡': 'Toyooka',
    '洲本': 'Sumoto', '淡路': 'Awaji', '丹波篠山': 'Tamba-Sasayama',
    '丹波': 'Tamba', 'たつの': 'Tatsuno', '赤穂': 'Ako',
    '大阪': 'Osaka', '豊中': 'Toyonaka', '吹田': 'Suita',
    '堺市': 'Sakai', '京都': 'Kyoto', '奈良': 'Nara',
    '和歌山': 'Wakayama', '東京': 'Tokyo', '横浜': 'Yokohama',
    '名古屋': 'Nagoya', '広島': 'Hiroshima', '岡山': 'Okayama',
}

HALL_CITIES = {
    '兵庫県立芸術文化センター': 'Nishinomiya',
    '松方ホール': 'Kobe', 'うはらホール': 'Kobe',
    '東灘区文化センター': 'Kobe', '灘区文化センター': 'Kobe',
    '北区文化センター': 'Kobe', '西区文化センター': 'Kobe',
    '垂水区文化センター': 'Kobe', '長田区文化センター': 'Kobe',
    '須磨区文化センター': 'Kobe', 'ピフレホール': 'Kobe',
    '新長田ピフレホール': 'Kobe', '葺合文化センター': 'Kobe',
    '北神区文化センター': 'Kobe', '神戸朝日ホール': 'Kobe',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def resolve_city(venue):
    for hint, city in HALL_CITIES.items():
        if hint in venue:
            return city
    for hint, city in CITY_HINTS.items():
        if hint in venue:
            return city
    return None


def listing_urls(session):
    urls = []
    page = 1
    while True:
        response = session.get(
            SCHEDULE_API,
            params={'per_page': 100, 'page': page, '_fields': 'id,link'},
            timeout=60,
        )
        response.raise_for_status()
        urls.extend(item['link'] for item in response.json() if item.get('link'))
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return list(dict.fromkeys(urls))


def parse_detail(page_html, url):
    soup = BeautifulSoup(page_html, 'html.parser')
    contents = soup.select_one('#contents-bk .contents') or soup.select_one('.contents')
    if not contents:
        return None

    title_node = contents.find('h2')
    title = clean_text(title_node).replace('\n', ' ')
    fields = {}
    for row in contents.select('table tr'):
        cells = row.select('th,td')
        if len(cells) >= 2:
            fields[clean_text(cells[0]).replace('\n', ' ')] = clean_text(cells[1]).replace('\n', ' ')

    raw_date = fields.get('日程', '')
    venue = fields.get('会場', '')
    date_match = re.search(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日', raw_date)
    city = resolve_city(venue)
    if not all((title, date_match, venue, city)):
        return None
    try:
        event_date = date(*map(int, date_match.groups())).isoformat()
    except ValueError:
        return None

    time_match = re.search(r'([0-2]?\d)\s*[:：]\s*([0-5]\d)\s*開演', raw_date)
    time_from = None
    if time_match and int(time_match.group(1)) < 24:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    # Keep the programme and editorial body, but omit pricing/ticket tables,
    # navigation, and scripts. Performer biographies remain useful context for
    # later programme extraction.
    for node in contents.select(
        'script, style, nav, form, table, .breadcrumb, .bread_crumb, '
        '.post-navigation, .wp-block-buttons'
    ):
        node.decompose()
    heading = contents.find('h2')
    if heading:
        heading.decompose()
    description = clean_text(contents)

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


class KobeEnsouJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kobe_ensou_jp',
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
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fetch_detail, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape Kobe ensemble concert detail',
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
    KobeEnsouJpCrawler().run()


if __name__ == '__main__':
    main()
