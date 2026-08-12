import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://thegrangefestival.co.uk/'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
SOURCE = 'The Grange Festival'
VENUE = 'The Grange'
CITY = 'Northington'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = {
    month: number
    for number, month in enumerate(
        (
            'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        ),
        start=1,
    )
}
DATE_GROUP_RE = re.compile(
    r'\b(\d{1,2}(?:\s*(?:,|&|and)\s*\d{1,2})*)\s+'
    rf'({"|".join(MONTHS)})\b',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def annual_programme_urls(session):
    index = BeautifulSoup(get_response(session, SITEMAP_URL).content, 'xml')
    sitemap_urls = [clean_text(node) for node in index.select('sitemap > loc')]
    programme_urls = []

    for sitemap_url in sitemap_urls:
        sitemap = BeautifulSoup(get_response(session, sitemap_url).content, 'xml')
        for node in sitemap.select('url > loc'):
            url = clean_text(node)
            if re.fullmatch(r'/20\d{2}/', urlparse(url).path):
                programme_urls.append(url)

    return list(dict.fromkeys(programme_urls))


def parse_dates(text, year):
    dates = []
    text = re.sub(r'£\s*\d[^\n]*', '', text)
    for match in DATE_GROUP_RE.finditer(text):
        month = MONTHS[match.group(2).title()]
        for day_text in re.findall(r'\d{1,2}', match.group(1)):
            try:
                event_date = date(year, month, int(day_text)).isoformat()
            except ValueError:
                continue
            if event_date not in dates:
                dates.append(event_date)
    return dates


def programme_items(content, programme_url):
    soup = BeautifulSoup(content, 'html.parser')
    year_match = re.search(r'/(20\d{2})/', urlparse(programme_url).path)
    if not year_match:
        return []
    year = int(year_match.group(1))
    items = []

    for block in soup.select('.block--size-c'):
        link = block.select_one('a[href*="/productions/"]')
        title_node = block.select_one('.card__title, h2, h3')
        title = clean_text(title_node)
        url = urljoin(programme_url, link.get('href', '')) if link else ''
        dates = parse_dates(clean_text(block), year)
        if title and url and dates:
            items.append((title, url, dates))

    return items


def detail_description(content):
    soup = BeautifulSoup(content, 'html.parser')
    main = soup.select_one('main')
    if not main:
        return None
    for node in main.select(
        '.section--hero, .section--production-title, .sidebar, '
        '.section--related, .section--cta'
    ):
        node.decompose()
    description = clean_text(main)
    return description or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)

    items = []
    for programme_url in annual_programme_urls(session):
        items.extend(programme_items(get_response(session, programme_url).content, programme_url))

    descriptions = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(get_response, session, url): url
            for _, url, _ in items
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = detail_description(future.result().content)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape The Grange Festival production detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                descriptions[url] = None

    records = []
    for title, url, dates in items:
        for event_date in dates:
            records.append(
                {
                    'title': title,
                    'date': event_date,
                    'url': url,
                    'time_from': None,
                    'venue': VENUE,
                    'city': CITY,
                    'country_code': 'GB',
                    'description': descriptions.get(url),
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                }
            )

    return sorted(records, key=lambda record: (record['date'], record['title'], record['url']))


class TheGrangeFestivalCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='thegrangefestival_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    TheGrangeFestivalCrawler().run()


if __name__ == '__main__':
    main()
