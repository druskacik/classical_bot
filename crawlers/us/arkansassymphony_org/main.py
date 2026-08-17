import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.arkansassymphony.org/'
SOURCE = 'Arkansas Symphony Orchestra'
SITEMAP_URL = f'{SOURCE_URL}event-sitemap.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b('
    r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|'
    r'Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2},\s+\d{4}'
    r')(?:\s*[-–]\s*('
    r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|'
    r'Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2},\s+\d{4}'
    r'))?\b',
    re.IGNORECASE,
)
STATE_ZIP_RE = re.compile(r'\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b')


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    normalized = value.replace('.', '')
    for pattern in ('%b %d, %Y', '%B %d, %Y'):
        try:
            return datetime.strptime(normalized, pattern).date()
        except ValueError:
            pass
    return None


def dates_from_page(soup):
    for node in soup.body.find_all(string=DATE_RE) if soup.body else []:
        match = DATE_RE.search(clean_text(node))
        if not match:
            continue
        start = parse_date(match.group(1))
        end = parse_date(match.group(2)) if match.group(2) else start
        if not start or not end or end < start or (end - start).days > 7:
            continue
        return [
            (start + timedelta(days=offset)).isoformat()
            for offset in range((end - start).days + 1)
        ]
    return []


def location_from_page(soup):
    box = soup.select_one('.event-location-box')
    if not box:
        return '', ''

    heading = box.select_one('h4')
    venue = clean_text(heading.get_text(' ', strip=True)) if heading else ''
    address = box.select_one('.location-address')
    lines = (
        [clean_text(line) for line in address.get_text('\n', strip=True).splitlines()]
        if address else []
    )
    city = ''
    for line in reversed(lines):
        if STATE_ZIP_RE.search(line):
            city = clean_text(line.split(',')[0])
            break
    return venue, city


def description_from_page(soup):
    content = soup.select_one('.fl-builder-content-primary')
    if not content:
        return None
    parts = []
    for module in content.select('.fl-module-rich-text'):
        if module.find_parent(id='tickets') or module.select_one('.event-location-box'):
            continue
        text = clean_text(module.get_text('\n', strip=True))
        if text and text not in parts and not DATE_RE.fullmatch(text):
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title_node = soup.select_one('h1')
    title = clean_text(title_node.get_text(' ', strip=True)) if title_node else ''
    dates = dates_from_page(soup)
    venue, city = location_from_page(soup)
    if not title or not dates or not venue or not city:
        return []

    description = description_from_page(soup)
    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for event_date in dates]


def event_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'xml')
    urls = []
    for node in soup.select('url > loc'):
        url = clean_text(node.get_text())
        if re.fullmatch(r'https://www\.arkansassymphony\.org/events/[^/]+/', url):
            urls.append(url)
    return list(dict.fromkeys(urls))


def fetch_event(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event_page(response.text, url)


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_event, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Event page request failed',
                    event='crawler_event_request_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No parseable concert events found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


class ArkansasSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='arkansassymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ArkansasSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
