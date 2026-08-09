import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'http://www.shphilharmonic.com/'
LIST_URL = urljoin(SOURCE_URL, 'activity')
SOURCE = 'Shanghai Philharmonic Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def canonical_event_url(href):
    url = urljoin(SOURCE_URL, href)
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def listing_page_count(soup):
    # Pagination is rendered client-side from these two plugin arguments.
    match = re.search(
        r'\.pagination\("(\d+)".*?items_per_page:\s*"?(\d+)',
        str(soup),
        re.DOTALL,
    )
    if not match:
        return 1
    total, per_page = map(int, match.groups())
    return max(1, (total + per_page - 1) // per_page)


def listing_items(soup):
    items = []
    for row in soup.select('ul.rows-list > li'):
        link = row.select_one('a[href*="/activity/"]')
        if not link:
            continue
        href = link.get('href')
        if href:
            items.append((canonical_event_url(href), row))
    return items


def parse_city(address):
    address = clean_text(address)
    if not address:
        return None
    # Municipalities omit a prefecture-level city; their municipality name is
    # the correct city. Other Chinese addresses normally expose a name ending 市.
    for municipality in ('上海', '北京', '天津', '重庆'):
        if address.startswith(municipality) or address.startswith(f'{municipality}市'):
            return municipality
    match = re.search(r'(?:省|自治区|特别行政区)?([^省自治区特别行政区]{2,12}市)', address)
    return match.group(1)[:-1] if match else None


def labelled_value(container, label):
    for element in container.select('p'):
        text = clean_text(element)
        if text.startswith(label):
            return text.split('：', 1)[-1].strip()
    return ''


def parse_record(detail, url, listing_row=None):
    title = clean_text(detail.select_one('.main-desc h1'))
    date_text = clean_text(detail.select_one('.main-desc .time1'))
    time_text = clean_text(detail.select_one('.main-desc .time2'))
    abstract = detail.select_one('.main-desc .desc-abstract')

    if listing_row is not None:
        title = title or clean_text(listing_row.select_one('h2'))
        date_text = date_text or clean_text(listing_row.select_one('.time1'))
        time_text = time_text or clean_text(listing_row.select_one('.time2'))
        abstract = abstract or listing_row.select_one('.info')

    venue = labelled_value(abstract, '场馆：') if abstract else ''
    address = labelled_value(abstract, '地址：') if abstract else ''
    city = parse_city(address)
    time_match = re.search(r'([01]\d|2[0-3]):[0-5]\d', time_text)

    try:
        event_date = date.fromisoformat(date_text).isoformat()
    except (TypeError, ValueError):
        return None

    if not title or not venue or not city or not url:
        return None

    description = clean_text(detail.select_one('#detail1 .desc')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_match.group(0) if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': 'CN',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_detail(session, url, listing_row):
    return parse_record(get_soup(session, url), url, listing_row)


class ShphilharmonicComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='shphilharmonic_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CN',
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

        first_page = get_soup(session, LIST_URL)
        items = listing_items(first_page)
        # The server uses one-based page parameters, while the JavaScript
        # pagination widget internally uses zero-based current-page indexes.
        for page in range(2, listing_page_count(first_page) + 1):
            items.extend(listing_items(get_soup(session, LIST_URL, params={'page': page})))

        unique_items = {url: row for url, row in items}
        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(scrape_detail, session, url, row): url
                for url, row in unique_items.items()
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    record = None
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    ShphilharmonicComCrawler().run()


if __name__ == '__main__':
    main()
