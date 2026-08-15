import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.teatromicaelense.pt/'
AGENDA_URL = urljoin(SOURCE_URL, 'agenda/')
SOURCE = 'Teatro Micaelense'
DEFAULT_VENUE = 'Teatro Micaelense'
DEFAULT_CITY = 'Ponta Delgada'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-PT,pt;q=0.9,en;q=0.7',
}

MONTHS = {
    'janeiro': 1,
    'fevereiro': 2,
    'março': 3,
    'marco': 3,
    'abril': 4,
    'maio': 5,
    'junho': 6,
    'julho': 7,
    'agosto': 8,
    'setembro': 9,
    'outubro': 10,
    'novembro': 11,
    'dezembro': 12,
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def build_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])[Hh:]([0-5]\d)\b', value)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_dates(value, url):
    """Return every explicitly advertised day, falling back to the dated URL."""
    url_match = re.search(r'/agenda/(20\d{2})-(\d{2})-(\d{2})/', url)
    if not url_match:
        return []
    year, url_month, url_day = map(int, url_match.groups())

    normalized = value.casefold().replace('–', '-').replace('—', '-')
    month_matches = list(re.finditer('|'.join(MONTHS), normalized))
    parsed = []
    for index, month_match in enumerate(month_matches):
        segment_start = month_matches[index - 1].end() if index else 0
        segment = normalized[segment_start:month_match.start()]
        month = MONTHS[month_match.group()]
        # Event labels use conjunctions/commas for multiple occurrences. A
        # hyphen denotes a range; expanding it preserves each advertised day.
        numbers = [int(number) for number in re.findall(r'\b\d{1,2}\b', segment)]
        if '-' in segment and len(numbers) >= 2:
            numbers = list(range(numbers[-2], numbers[-1] + 1))
        for day in numbers:
            try:
                parsed.append(date(year, month, day).isoformat())
            except ValueError:
                continue

    if not parsed:
        try:
            parsed = [date(year, url_month, url_day).isoformat()]
        except ValueError:
            return []
    return list(dict.fromkeys(parsed))


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title_element = soup.select_one('.flex-1 h1')
    title = clean_text(title_element)
    content = title_element.parent if title_element else None
    when_element = title_element.find_next_sibling('div') if title_element else None
    when = clean_text(when_element)
    dates = parse_dates(when, url)
    description = clean_text(content.select_one('.prose.maxcontent')) if content else ''

    if not title or not dates:
        log_message(
            'Skipping Teatro Micaelense event with missing required fields',
            event='crawler_record_skipped',
            level='warning',
            url=url,
            has_title=bool(title),
            has_date=bool(dates),
        )
        return []

    common = {
        'title': title,
        'url': url,
        'time_from': parse_time(when),
        'venue': DEFAULT_VENUE,
        'city': DEFAULT_CITY,
        'country_code': 'PT',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }
    return [{**common, 'date': event_date} for event_date in dates]


def extract_event_urls(soup):
    urls = []
    for link in soup.select('a[href]'):
        url = urljoin(SOURCE_URL, link.get('href', ''))
        if re.search(r'/agenda/20\d{2}-\d{2}-\d{2}/[^/]+/?$', urlparse(url).path):
            urls.append(url)
    return list(dict.fromkeys(urls))


class TeatroMicaelensePtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatromicaelense_pt',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PT',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = build_session()
        event_urls = []

        # Current and archive modes are distinct server-rendered feeds. Follow
        # their own next links so mode=arquivo remains attached to every page.
        for first_url in (AGENDA_URL, f'{AGENDA_URL}?mode=arquivo'):
            page_url = first_url
            visited_pages = set()
            while page_url and page_url not in visited_pages:
                visited_pages.add(page_url)
                try:
                    response = session.get(page_url, timeout=45)
                    response.raise_for_status()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Teatro Micaelense agenda page',
                        event='crawler_fetch_failed',
                        level='error',
                        url=page_url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    raise
                soup = BeautifulSoup(response.text, 'html.parser')
                event_urls.extend(extract_event_urls(soup))
                next_link = soup.select_one('#next_page_link')
                page_url = urljoin(page_url, next_link['href']) if next_link else None

        event_urls = list(dict.fromkeys(event_urls))

        def fetch_detail(url):
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                return parse_detail(response.text, url)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Teatro Micaelense event',
                    event='crawler_fetch_failed',
                    level='error',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

        with ThreadPoolExecutor(max_workers=8) as executor:
            nested_records = executor.map(fetch_detail, event_urls)
            records = [record for group in nested_records for record in group]

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    TeatroMicaelensePtCrawler().run()


if __name__ == '__main__':
    main()
