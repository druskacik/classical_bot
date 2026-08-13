import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.toppanhall.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'concert/list.html')
SOURCE = 'TOPPAN Hall'
VENUE = 'TOPPAN Hall'
CITY = 'Tokyo'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def calendar_year_range(html):
    match = re.search(
        r"['\"]s['\"]\s*:\s*['\"](\d{4})['\"].*?"
        r"['\"]e['\"]\s*:\s*['\"](\d{4})['\"]",
        html,
        re.DOTALL,
    )
    if not match:
        raise ValueError('Could not determine TOPPAN Hall calendar year range')
    start_year, end_year = map(int, match.groups())
    return range(start_year, end_year + 1)


def listing_urls(session):
    response = session.get(CALENDAR_URL, timeout=45)
    response.raise_for_status()
    years = calendar_year_range(response.text)
    months = [f'{year}-{month:02d}' for year in years for month in range(1, 13)]
    urls = set()
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(fetch_listing, month) for month in months]
        for future in as_completed(futures):
            urls.update(future.result())
    return sorted(urls)


def fetch_listing(month):
    response = requests.get(
        CALENDAR_URL,
        params={'ym': month},
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    return {
        urljoin(response.url, link['href'])
        for link in soup.select('a[href*="detail/"][href]')
    }


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title_node = soup.select_one('h2.hl02')
    date_node = soup.select_one('.info .txt01 span')
    time_node = soup.select_one('.info .txt02 span')
    title = clean_text(title_node).replace('\n', ' ')
    raw_date = clean_text(date_node)
    date_match = re.fullmatch(r'(\d{4})/(\d{1,2})/(\d{1,2})', raw_date)
    if not title or not date_match:
        return None
    try:
        event_date = date(*map(int, date_match.groups())).isoformat()
    except ValueError:
        return None

    time_from = None
    time_match = re.fullmatch(r'([0-2]?\d):([0-5]\d)', clean_text(time_node))
    if time_match and int(time_match.group(1)) < 24:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    # Preserve artistic notes, performers, and the full programme while omitting
    # contact, ticketing, navigation, and sharing text.
    event_section = title_node.find_parent('section') if title_node else None
    if event_section:
        for node in event_section.select(
            '.contact_list, .ticket_box, script, style, nav, form, iframe'
        ):
            node.decompose()
    description = clean_text(event_section)

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': VENUE,
        'city': CITY,
        'country_code': 'JP',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_detail(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    # The server omits a charset header even though the document declares UTF-8.
    response.encoding = 'utf-8'
    return parse_detail(response.text, url)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch_detail, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape TOPPAN Hall concert detail',
                    event='crawler_detail_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class ToppanhallComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='toppanhall_com',
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
    ToppanhallComCrawler().run()


if __name__ == '__main__':
    main()
