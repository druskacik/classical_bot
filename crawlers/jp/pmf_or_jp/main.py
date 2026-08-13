import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.pmf.or.jp/'
SOURCE = 'Pacific Music Festival Sapporo'
SCHEDULE_URL = urljoin(SOURCE_URL, 'jp/schedule/')
ARCHIVE_URLS = (
    urljoin(SOURCE_URL, 'jp/archive/'),
    urljoin(SOURCE_URL, 'jp/archive/2010.html'),
    urljoin(SOURCE_URL, 'jp/archive/2000.html'),
    urljoin(SOURCE_URL, 'jp/archive/1990.html'),
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.5',
}

# PMF is based in Sapporo but regularly tours. These hints deliberately cover
# explicit touring places before the conservative set of known Sapporo venues.
CITY_HINTS = {
    '東京': 'Tokyo', 'サントリーホール': 'Tokyo',
    '苫小牧': 'Tomakomai', '小樽': 'Otaru', '奈井江': 'Naie',
    '江別': 'Ebetsu', '千歳': 'Chitose', '岩見沢': 'Iwamizawa',
    '帯広': 'Obihiro', '釧路': 'Kushiro', '函館': 'Hakodate',
    '旭川': 'Asahikawa', '北広島': 'Kitahiroshima', '恵庭': 'Eniwa',
    '横浜': 'Yokohama', '大阪': 'Osaka', '名古屋': 'Nagoya',
}
SAPPORO_VENUE_HINTS = (
    '札幌', 'Kitara', 'キタラ', '芸術の森', '豊平館', '赤れんが庁舎',
    '北海道庁', 'JR TOWER', 'JRタワー', 'AOAO SAPPORO', '日本生命札幌ビル',
    '清田区民センター', '市民交流プラザ', '時計台', '大通公園',
)


def clean_text(value):
    if value is None:
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
    if any(hint in venue for hint in SAPPORO_VENUE_HINTS):
        return 'Sapporo'
    return None


def fetch_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def discover_year_pages(session):
    pages = {SCHEDULE_URL}
    for archive_url in ARCHIVE_URLS:
        try:
            soup = fetch_soup(session, archive_url)
        except requests.RequestException as error:
            log_message(
                'Failed to inspect PMF archive page', event='crawler_listing_fetch_failed',
                level='warning', url=archive_url, error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        for anchor in soup.select('a[href]'):
            url = urljoin(archive_url, anchor.get('href'))
            if re.fullmatch(r'https://www\.pmf\.or\.jp/jp/schedule/(?:\d{4}/)?', url):
                pages.add(url)
    return sorted(pages)


def discover_detail_urls(session):
    urls = set()
    for page_url in discover_year_pages(session):
        try:
            soup = fetch_soup(session, page_url)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch PMF schedule page', event='crawler_listing_fetch_failed',
                level='warning', url=page_url, error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        for anchor in soup.select('a[href]'):
            url = urljoin(page_url, anchor.get('href'))
            path = urlparse(url).path
            if re.fullmatch(r'/jp/schedule/[^/]+/[^/]+\.html', path):
                urls.add(url)
    return sorted(urls)


def labelled_container(soup, label):
    for node in soup.select('.scheduleDetailCont'):
        text = clean_text(node)
        if text == label or text.startswith(label + '\n') or text.startswith(label + ' '):
            return node
    return None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    detail = soup.select_one('.scheduleDetail')
    intro = soup.select_one('.scheduleIntro')
    if detail is None:
        return None

    detail_text = clean_text(detail)
    date_match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', detail_text)
    if not date_match:
        return None
    try:
        event_date = date(*map(int, date_match.groups())).isoformat()
    except ValueError:
        return None

    title_node = soup.select_one('.scheduleIntro h1, .scheduleIntro h2, h1')
    if title_node:
        for badge in title_node.select('.category, .optionIcon'):
            badge.decompose()
    title = clean_text(title_node).replace('\n', ' ')
    if not title:
        # Calendar/dialog text starts with the event title immediately before date.
        title_match = re.search(r'(?:開催日\n)+(.*?)\n\d{4}年', detail_text, re.S)
        title = clean_text(title_match.group(1)).replace('\n', ' ') if title_match else ''

    venue_node = labelled_container(soup, '会場')
    venue = clean_text(venue_node)
    venue = re.sub(r'^会場\s*', '', venue).replace('\n> 詳細をみる', '').strip()
    city = resolve_city(venue)
    if not all((title, venue, city)):
        return None

    time_node = labelled_container(soup, '時間')
    time_text = clean_text(time_node)
    time_match = re.search(r'開演\s*([0-2]?\d):([0-5]\d)', time_text)
    time_from = None
    if time_match and int(time_match.group(1)) < 24:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    description_parts = []
    intro_text = clean_text(intro)
    if intro_text:
        description_parts.append(intro_text)
    for node in soup.select('.scheduleDetailCont.artist, .scheduleDetailCont.music'):
        text = clean_text(node)
        if text:
            description_parts.append(text)
    description = '\n\n'.join(dict.fromkeys(description_parts)) or None

    return {
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
    }


def fetch_detail(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return parse_detail(response.text, url)


class PmfOrJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='pmf_or_jp',
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
        urls = discover_detail_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(fetch_detail, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape PMF schedule detail',
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
    PmfOrJpCrawler().run()


if __name__ == '__main__':
    main()
