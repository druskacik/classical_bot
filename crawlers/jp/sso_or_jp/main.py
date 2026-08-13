import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sso.or.jp/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts/')
SOURCE = 'Sapporo Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja,en;q=0.7',
}

SEASON_START_YEARS = range(2019, date.today().year + 1)

HOME_VENUE_TERMS = (
    'Kitara', 'キタラ', '札幌文化芸術劇場', '札幌市教育文化会館',
    '札幌市民ホール', 'カナモトホール', '札幌芸術の森', '大通公園',
    '札幌コンサートホール',
)

# Venues which do not include their municipality in their printed name, plus
# recurring tour halls found in the published archive.
VENUE_CITIES = {
    'サントリーホール': '東京',
    '東京オペラシティ': '東京',
    '東京芸術劇場': '東京',
    'ミューザ川崎': '川崎',
    '豊田市コンサートホール': '豊田',
    '札幌ドーム': '札幌',
    '真駒内': '札幌',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    response.encoding = 'utf-8'
    return BeautifulSoup(response.text, 'html.parser')


def listing_urls():
    urls = [CONCERTS_URL]
    urls.extend(
        urljoin(CONCERTS_URL, f'{year}-{year + 1}/')
        for year in SEASON_START_YEARS
    )
    return urls


def listing_items(session):
    items = {}
    for page_url in listing_urls():
        try:
            soup = get_soup(session, page_url)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape SSO concert listing',
                event='crawler_page_failed', level='warning', url=page_url,
                error_type=type(error).__name__, error_message=str(error),
            )
            continue
        for wrapper in soup.select('.ConcertListWrapper'):
            link = wrapper.select_one('a[href]')
            if not link:
                continue
            url = urljoin(page_url, link.get('href'))
            if re.search(r'/concerts/\d{4}/\d{2}/[^/]+/$', url):
                items[url] = wrapper
    return items


def parse_occurrences(text):
    occurrences = []
    pattern = re.compile(
        r'(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日'
        r'(?:（[^）]*）)?\s*(?:(\d{1,2}):(\d{2}))?'
    )
    for match in pattern.finditer(text):
        try:
            event_date = date(
                int(match.group(1)), int(match.group(2)), int(match.group(3))
            ).isoformat()
        except ValueError:
            continue
        event_time = (
            f'{int(match.group(4)):02d}:{match.group(5)}'
            if match.group(4) else None
        )
        occurrence = (event_date, event_time)
        if occurrence not in occurrences:
            occurrences.append(occurrence)
    return occurrences


def resolve_city(title, venue):
    for term in HOME_VENUE_TERMS:
        if term.lower() in venue.lower():
            return '札幌'
    for term, city in VENUE_CITIES.items():
        if term.lower() in venue.lower():
            return city

    # Japanese public halls commonly begin with an explicit municipality.
    match = re.match(r'([^\s　]{1,12}?)(市|町|村)(?:立|民|文化|交流|総合|公民|生涯|スポーツ)', venue)
    if match:
        return match.group(1) + match.group(2)

    # Tour titles consistently identify their destination before 公演/演奏会.
    match = re.search(
        r'(?:札幌交響楽団|札響)[ \u3000]*([^（）\s]{2,12}?)(?:公演|演奏会|コンサート)',
        title,
    )
    if match and match.group(1) not in {'定期', '特別', '招待'}:
        return match.group(1).removesuffix('市').removesuffix('町').removesuffix('村')
    return None


def parse_listing_item(wrapper, url):
    title = clean_text(wrapper.select_one('h4'))
    entries = wrapper.select('li')
    date_text = clean_text(entries[0]) if entries else ''
    venue = clean_text(entries[1]) if len(entries) > 1 else ''
    venue = re.sub(r'^会場[：:]\s*', '', venue).strip()
    city = resolve_city(title, venue)
    if not title or not venue or not city:
        return []
    return [
        {
            'title': title, 'date': event_date, 'url': url,
            'time_from': event_time, 'venue': venue, 'city': city,
            'country_code': 'JP', 'description': None,
            'source_url': SOURCE_URL, 'source': SOURCE,
        }
        for event_date, event_time in parse_occurrences(date_text)
    ]


def add_detail(session, url, records):
    soup = get_soup(session, url)
    detail = soup.select_one('#ConcertWrapper .innerBox')
    description = clean_text(detail)
    if description:
        for record in records:
            record['description'] = description
    return records


class SsoOrJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sso_or_jp',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        parsed = {
            url: records
            for url, wrapper in listing_items(session).items()
            if (records := parse_listing_item(wrapper, url))
        }
        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(add_detail, session, url, page_records): url
                for url, page_records in parsed.items()
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape SSO concert detail',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    records.extend(parsed[url])
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    SsoOrJpCrawler().run()


if __name__ == '__main__':
    main()
