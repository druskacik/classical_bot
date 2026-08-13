import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup, Comment

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://nipponsteel-kioihall.jp/'
SOURCE = 'Nippon Steel Kioi Hall'
SITEMAP_URL = f'{SOURCE_URL}wp-sitemap-posts-concert-1.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

HOME_VENUES = ('日本製鉄紀尾井ホール', '紀尾井ホール', '日本製鉄紀尾井小ホール', '紀尾井小ホール')
CITY_HINTS = {
    '東京': 'Tokyo', '千代田': 'Tokyo', '紀尾井': 'Tokyo',
    '名古屋': 'Nagoya', '愛知': 'Nagoya',
    '大阪': 'Osaka', '京都': 'Kyoto', '横浜': 'Yokohama',
    '川崎': 'Kawasaki', '神戸': 'Kobe', '札幌': 'Sapporo',
    '福岡': 'Fukuoka', '仙台': 'Sendai', '広島': 'Hiroshima',
    '長野': 'Nagano', '金沢': 'Kanazawa', '新潟': 'Niigata',
    '高崎': 'Takasaki', '水戸': 'Mito', '浜松': 'Hamamatsu',
    'しらかわホール': 'Nagoya', 'いずみホール': 'Osaka',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def sitemap_urls(session):
    response = session.get(SITEMAP_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    return [node.get_text(strip=True) for node in soup.select('url > loc')]


def table_value(soup, heading):
    for row in soup.select('.p-concert-detail-host__info-table-row'):
        head = row.select_one('th')
        body = row.select_one('td')
        if head and body and clean_text(head).startswith(heading):
            return clean_text(body)
    return ''


def resolve_city(venue, title):
    if any(home in venue for home in HOME_VENUES):
        return 'Tokyo'
    for hint, city in CITY_HINTS.items():
        if hint in venue:
            return city
    # Touring titles often name the city while retaining the orchestra's Kioi
    # Hall name, so Tokyo/home-name hints must not override that evidence.
    for hint, city in CITY_HINTS.items():
        if city != 'Tokyo' and hint in title:
            return city
    return None


def parse_detail(html, url):
    # The site's visible genre checkboxes are disabled, but every detail retains
    # its first-party taxonomy label in this commented label element.
    soup = BeautifulSoup(html, 'html.parser')
    content = soup.select_one('.p-concert-detail-host__contents')
    genre_comments = content.find_all(
        string=lambda value: isinstance(value, Comment) and 'c-label--genre' in value
    ) if content else []
    genres = [clean_text(BeautifulSoup(str(comment), 'html.parser')) for comment in genre_comments]
    if 'クラシック' not in genres:
        return None
    title_node = soup.select_one('.p-concert-detail-host__intro-title')
    raw_datetime = table_value(soup, '日時')
    if not raw_datetime:
        raw_datetime = clean_text(soup.select_one('.p-concert-detail-host__intro-date-text'))
    venue = table_value(soup, '会場')
    if not venue:
        venue = clean_text(soup.select_one('.p-concert-detail-host__intro-hall'))
    title = clean_text(title_node).replace('\n', ' ')

    date_match = re.search(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日', raw_datetime)
    if not all((title, date_match, venue)):
        return None
    try:
        event_date = date(*map(int, date_match.groups())).isoformat()
    except ValueError:
        return None

    time_match = re.search(r'([01]?\d|2[0-3])\s*時\s*([0-5]?\d)\s*分', raw_datetime)
    time_from = None
    if time_match:
        time_from = f'{int(time_match.group(1)):02d}:{int(time_match.group(2)):02d}'

    description = clean_text(content)
    city = resolve_city(venue, title)
    if not city:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue.replace('\n', ' '),
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


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = sitemap_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fetch_detail, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Nippon Steel Kioi Hall concert detail',
                    event='crawler_detail_fetch_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class NipponsteelKioihallJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nipponsteel_kioihall_jp',
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
    NipponsteelKioihallJpCrawler().run()


if __name__ == '__main__':
    main()
