import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.geidai.ac.jp/'
ARCHIVE_URL = urljoin(SOURCE_URL, 'event/sogakudo')
SOURCE = 'Tokyo University of the Arts'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

VENUE_CITIES = {
    '東京藝術大学奏楽堂': 'Tokyo',
    '奏楽堂': 'Tokyo',
    '第１ホール': 'Tokyo',
    '第2ホール': 'Tokyo',
    '第２ホール': 'Tokyo',
    '第3ホール': 'Tokyo',
    '第３ホール': 'Tokyo',
    '第4ホール': 'Tokyo',
    '第４ホール': 'Tokyo',
    '第5ホール': 'Tokyo',
    '第５ホール': 'Tokyo',
    '第6ホール': 'Tokyo',
    '第６ホール': 'Tokyo',
}

CITY_HINTS = {
    '東京都': 'Tokyo', '横浜市': 'Yokohama', '川崎市': 'Kawasaki',
    '千葉市': 'Chiba', 'さいたま市': 'Saitama', '取手市': 'Toride',
    '京都市': 'Kyoto', '大阪市': 'Osaka', '名古屋市': 'Nagoya',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def archive_pages(session):
    response = session.get(ARCHIVE_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    urls = {
        canonical_url(urljoin(ARCHIVE_URL, anchor.get('href')))
        for anchor in soup.select('a[href]')
        if re.search(r'/event/sogakudo/sogakudo_\d{4}', anchor.get('href', ''))
    }
    return sorted(urls)


def detail_urls(session):
    urls = set()
    for archive_url in archive_pages(session):
        response = session.get(archive_url, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        urls.update(
            canonical_url(urljoin(archive_url, anchor.get('href')))
            for anchor in soup.select('a[href*="/container/sogakudo/"]')
        )
    return sorted(urls)


def labelled_value(container, labels):
    for row in container.select('tr'):
        cells = row.find_all(['th', 'td'], recursive=False)
        if len(cells) < 2:
            continue
        label = clean_text(cells[0]).replace('\n', '')
        if any(label.startswith(candidate) for candidate in labels):
            return clean_text(cells[1])
    return ''


def extract_dates(raw_date):
    text = raw_date.replace('（', '(').replace('）', ')')
    # Remove weekday annotations so their digits cannot affect the date scan.
    text = re.sub(r'\([月火水木金土日]\)', '', text)
    found = []
    year = month = None
    token_pattern = re.compile(
        r'(?:(?P<year>20\d{2})\s*年\s*)?'
        r'(?:(?P<month>1[0-2]|0?[1-9])\s*月\s*)?'
        r'(?P<day>3[01]|[12]\d|0?[1-9])\s*日'
    )
    for match in token_pattern.finditer(text):
        if match.group('year'):
            year = int(match.group('year'))
        if match.group('month'):
            month = int(match.group('month'))
        if year is None or month is None:
            continue
        try:
            value = date(year, month, int(match.group('day'))).isoformat()
        except ValueError:
            continue
        if value not in found:
            found.append(value)
    return found


def resolve_city(venue, content_text):
    evidence = f'{venue}\n{content_text}'
    for hint, city in CITY_HINTS.items():
        if hint in evidence:
            return city
    for hint, city in VENUE_CITIES.items():
        if hint in venue:
            return city
    return None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1.tua_template_type_news_h1')).replace('\n', ' ')
    content = soup.select_one('.tua_main_subpage')
    if not title or not content:
        return []

    raw_date = labelled_value(content, ('日時', '開催日時'))
    venue = labelled_value(content, ('会場', '場所')).replace('\n', ' ')
    dates = extract_dates(raw_date)
    content_text = clean_text(content)
    city = resolve_city(venue, content_text)
    if not dates or not venue or not city:
        return []

    time_match = re.search(
        r'(?:開演\s*)?([01]?\d|2[0-3])\s*[:：]\s*([0-5]\d)\s*開演', raw_date
    )
    if not time_match:
        time_match = re.search(
            r'開演\s*([01]?\d|2[0-3])\s*[:：]\s*([0-5]\d)', raw_date
        )
    time_from = (
        f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
        if time_match else None
    )

    for node in content.select('script, style, nav, form, .tua_main_back_to_list'):
        node.decompose()
    description = clean_text(content)
    return [
        {
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
        for event_date in dates
    ]


def fetch_detail(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return parse_detail(response.text, url)


class GeidaiAcJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='geidai_ac_jp',
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
        urls = detail_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch_detail, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Tokyo University of the Arts concert detail',
                        event='crawler_detail_fetch_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    GeidaiAcJpCrawler().run()


if __name__ == '__main__':
    main()
