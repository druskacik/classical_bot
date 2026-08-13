import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ryukyusymphony.or.jp/'
CONCERT_URL = urljoin(SOURCE_URL, 'concert/')
SOURCE = 'Ryukyu Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

# The orchestra tours Okinawa's islands, so a Naha default would be unsafe.
# These first-party venue and municipality strings make the location explicit.
CITY_HINTS = {
    '那覇': 'Naha',
    '浦添': 'Urasoe',
    '沖縄市': 'Okinawa',
    '宜野湾': 'Ginowan',
    '豊見城': 'Tomigusuku',
    '糸満': 'Itoman',
    '南城': 'Nanjo',
    'うるま': 'Uruma',
    '名護': 'Nago',
    '宮古島': 'Miyakojima',
    '石垣': 'Ishigaki',
    '久米島': 'Kumejima',
    '南大東': 'Minamidaito',
    '北大東': 'Kitadaito',
    '読谷': 'Yomitan',
    '西原': 'Nishihara',
    '北谷': 'Chatan',
    '嘉手納': 'Kadena',
    '金武': 'Kin',
    '本部': 'Motobu',
    '今帰仁': 'Nakijin',
    '恩納': 'Onna',
    '与那原': 'Yonabaru',
    '南風原': 'Haebaru',
    '八重瀬': 'Yaese',
    '竹富': 'Taketomi',
    '与那国': 'Yonaguni',
    '国頭': 'Kunigami',
    '大宜味': 'Ogimi',
    '東村': 'Higashi',
    '伊江': 'Ie',
    '伊平屋': 'Iheya',
    '伊是名': 'Izena',
    '多良間': 'Tarama',
    '渡嘉敷': 'Tokashiki',
    '座間味': 'Zamami',
    '粟国': 'Aguni',
    '渡名喜': 'Tonaki',
    '国立劇場おきなわ': 'Urasoe',
    'アイム・ユニバース てだこホール': 'Urasoe',
    'てだこホール': 'Urasoe',
    '沖縄コンベンションセンター': 'Ginowan',
    'パレット市民劇場': 'Naha',
    'タイムスホール': 'Naha',
    '琉球新報ホール': 'Naha',
}

DATE_RE = re.compile(r'(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日')
TIME_RE = re.compile(
    r'(?:(?:開演|START)\s*[:：]?\s*([0-2]?\d)\s*[:：]\s*([0-5]\d)'
    r'|([0-2]?\d)\s*[:：]\s*([0-5]\d)\s*開演)',
    re.I,
)
VENUE_RE = re.compile(r'(?:場所|会場)\s*[:：]\s*([^\n]+)')


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = unicodedata.normalize('NFKC', text)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(text):
    match = DATE_RE.search(text)
    if not match:
        return None
    try:
        return date(*map(int, match.groups())).isoformat()
    except ValueError:
        return None


def resolve_city(venue, description):
    evidence = f'{venue}\n{description}'
    for hint, city in CITY_HINTS.items():
        if hint in evidence:
            return city
    return None


def listing_urls(session):
    urls = set()
    page = 1
    while True:
        url = CONCERT_URL if page == 1 else urljoin(CONCERT_URL, f'page/{page}/')
        response = session.get(url, timeout=60)
        if response.status_code == 404:
            break
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        page_urls = {
            urljoin(SOURCE_URL, anchor['href'])
            for anchor in soup.select('.posts .post > a[href]')
        }
        if not page_urls or page_urls.issubset(urls):
            break
        urls.update(page_urls)
        page += 1
    return sorted(urls)


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article#concert')
    content = article.select_one('.content') if article else None
    title_node = article.select_one('.column.posts.single > .title') if article else None
    if article is None or content is None or title_node is None:
        return None

    title = clean_text(title_node).replace('\n', ' ')
    description = clean_text(content)
    venue_match = VENUE_RE.search(description)
    if not venue_match:
        return None
    venue = venue_match.group(1).strip(' /◆★・')
    # Stop at common inline labels when the page omitted a line break.
    venue = re.split(r'\s+(?:出演|曲目|開場|開演|日時)\s*[:：]', venue)[0].strip()
    event_date = parse_date(description)
    city = resolve_city(venue, description)
    if not all((title, event_date, venue, city)):
        return None

    time_match = TIME_RE.search(description)
    time_from = None
    if time_match:
        hour = time_match.group(1) or time_match.group(3)
        minute = time_match.group(2) or time_match.group(4)
        if int(hour) < 24:
            time_from = f'{int(hour):02d}:{minute}'

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


def fetch_detail(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return parse_detail(response.text, url)


class RyukyuSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ryukyusymphony_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = listing_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch_detail, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Ryukyu Symphony concert detail',
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
    RyukyuSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
