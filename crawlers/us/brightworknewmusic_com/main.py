import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://brightworknewmusic.com/'
EVENTS_URL = f'{SOURCE_URL}events/'
SOURCE = 'Brightwork newmusic'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(r'^[A-Z][a-z]+ \d{1,2}, \d{4}$')
TIME_RE = re.compile(r'^(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?$', re.I)
US_STATE_RE = re.compile(
    r'^(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b',
    re.I,
)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), '%B %d, %Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(value):
    match = TIME_RE.match(clean_text(value))
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.lower() == 'p' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def listing_items(html):
    soup = BeautifulSoup(html, 'html.parser')
    items = []
    seen = set()
    for card in soup.select('.uabb-blog-post-content'):
        link = card.select_one('h3.uabb-post-heading a[href*="/event/"]')
        date_node = card.select_one('.uabb-meta-date')
        meta = date_node.parent if date_node else None
        spans = meta.find_all('span', recursive=False) if meta else []
        venue = clean_text(spans[-1].get_text(' ', strip=True)) if len(spans) > 1 else ''
        if not link or not date_node or not venue:
            continue
        url = link.get('href', '').split('#', 1)[0]
        event_date = parse_date(date_node.get_text(' ', strip=True))
        if not event_date or url in seen:
            continue
        seen.add(url)
        items.append({
            'title': clean_text(link.get_text(' ', strip=True)),
            'date': event_date,
            'url': url,
            'venue': venue,
        })
    return items


def city_from_sidebar(sidebar):
    for line in sidebar.stripped_strings:
        parts = [clean_text(part) for part in clean_text(line).split(',')]
        if len(parts) >= 2 and US_STATE_RE.match(parts[-1]):
            return parts[-2]
    return ''


def parse_detail(item, html):
    soup = BeautifulSoup(html, 'html.parser')
    description_node = soup.select_one('#event-description .fl-rich-text')
    description = (
        description_node.get_text('\n', strip=True) if description_node else None
    )

    sidebar = None
    for column in soup.select('.fl-col-small .fl-col-content'):
        if item['venue'].casefold() in clean_text(column.get_text(' ', strip=True)).casefold():
            sidebar = column
            break
    if not sidebar:
        return None

    city = city_from_sidebar(sidebar)
    if not city:
        return None

    time_from = None
    for node in sidebar.select('.fl-html'):
        time_from = parse_time(node.get_text(' ', strip=True))
        if time_from:
            break

    return {
        **item,
        'time_from': time_from,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_detail(item):
    response = requests.get(item['url'], headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_detail(item, response.text)


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(EVENTS_URL, timeout=60)
    response.raise_for_status()
    items = listing_items(response.text)

    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_detail, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                record = future.result()
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipping event without a parseable city or venue',
                        event='crawler_event_skipped',
                        level='warning',
                        url=item['url'],
                    )
            except requests.RequestException as error:
                log_message(
                    'Event detail request failed',
                    event='crawler_detail_failed',
                    level='warning',
                    url=item['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No parseable events found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


class BrightworkNewmusicComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='brightworknewmusic_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        return scrape_concerts()


def main():
    BrightworkNewmusicComCrawler().run()


if __name__ == '__main__':
    main()
