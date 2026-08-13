import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sendaiphil.jp/'
CONCERT_URL = urljoin(SOURCE_URL, 'concert/')
ARCHIVE_URL = urljoin(SOURCE_URL, 'pages/285/')
SOURCE = 'Sendai Philharmonic Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

CITY_HINTS = {
    '仙台': 'Sendai',
    'イズミティ21': 'Sendai',
    '東京エレクトロンホール宮城': 'Sendai',
    '東北大学百周年記念会館 川内萩ホール': 'Sendai',
    '岩沼': 'Iwanuma',
    '名取': 'Natori',
    '大和町': 'Taiwa',
    '登米': 'Tome',
    '福島市': 'Fukushima',
    'いわき': 'Iwaki',
    '郡山': 'Koriyama',
    '秋田': 'Akita',
    'サントリーホール': 'Tokyo',
    '川崎': 'Kawasaki',
}

FULL_DATE_RE = re.compile(r'(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日')
SHORT_DATE_RE = re.compile(r'(?<!月)(\d{1,2})日')
TIME_RE = re.compile(r'([0-2]?\d)\s*[:：]\s*([0-5]\d)\s*開\s*演')
MONTH_TITLE_RE = re.compile(r'20\d{2}年\d{1,2}月')


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
    return None


def parse_date_line(text, previous_year=None, previous_month=None):
    full = FULL_DATE_RE.search(text)
    if not full:
        return [], previous_year, previous_month
    year, month, day = map(int, full.groups())
    occurrences = [(year, month, day, full.start())]
    for match in SHORT_DATE_RE.finditer(text, full.end()):
        occurrences.append((year, month, int(match.group(1)), match.start()))

    parsed = []
    for index, (item_year, item_month, item_day, offset) in enumerate(occurrences):
        end = occurrences[index + 1][3] if index + 1 < len(occurrences) else len(text)
        # Search from the date through the next date segment. This deliberately
        # ignores opening times, which appear after the performance start time.
        segment = text[offset:end]
        time_match = TIME_RE.search(segment)
        try:
            event_date = date(item_year, item_month, item_day).isoformat()
        except ValueError:
            continue
        time_from = None
        if time_match and int(time_match.group(1)) < 24:
            time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
        parsed.append((event_date, time_from))
    return parsed, year, month


def parse_card(card, page_url):
    title_node = card.select_one('.type008-title')
    body = card.select_one('.type008-txt')
    if not title_node or not body:
        return []
    title = clean_text(title_node).replace('\n', ' ')
    description = clean_text(body)
    link = card.find_parent('a', href=True)
    url = urljoin(page_url, link['href']) if link else page_url

    records = []
    pending_dates = []
    venue_follows = False
    year = month = None
    for node in body.find_all('div', recursive=False):
        line = clean_text(node).replace('\n', ' ')
        if '\uf073' in line or FULL_DATE_RE.search(line):
            pending_dates, year, month = parse_date_line(line, year, month)
            venue_follows = False
        elif ('\uf041' in line or line.startswith('')) and pending_dates:
            venue = line.replace('\uf041', '').replace('', '').strip()
            if not venue:
                venue_follows = True
                continue
            city = resolve_city(venue)
            if not city:
                pending_dates = []
                continue
            for event_date, time_from in pending_dates:
                records.append({
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
                })
            pending_dates = []
            venue_follows = False
        elif venue_follows and pending_dates and line:
            venue = line
            city = resolve_city(venue)
            if not city:
                pending_dates = []
                continue
            for event_date, time_from in pending_dates:
                records.append({
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
                })
            pending_dates = []
            venue_follows = False
    return records


def archive_pages(session):
    response = session.get(ARCHIVE_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    urls = []
    for link in soup.select('a[href]'):
        if MONTH_TITLE_RE.fullmatch(clean_text(link)):
            urls.append(urljoin(ARCHIVE_URL, link['href']))
    return list(dict.fromkeys(urls))


def parse_listing(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for card in soup.select('.type008-box'):
        records.extend(parse_card(card, page_url))
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = [CONCERT_URL]
    try:
        urls.extend(archive_pages(session))
    except requests.RequestException as error:
        log_message(
            'Failed to discover Sendai Philharmonic archive pages',
            event='crawler_archive_discovery_failed', level='warning', url=ARCHIVE_URL,
            error_type=type(error).__name__, error_message=str(error),
        )

    records = []
    for url in urls:
        try:
            response = session.get(url, timeout=60)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Sendai Philharmonic concert listing',
                event='crawler_listing_fetch_failed', level='warning', url=url,
                error_type=type(error).__name__, error_message=str(error),
            )
            continue
        records.extend(parse_listing(response.text, url))
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class SendaiphilJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sendaiphil_jp',
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
    SendaiphilJpCrawler().run()


if __name__ == '__main__':
    main()
