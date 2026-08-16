import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://fcsymphony.org/'
EVENT_API_URL = f'{SOURCE_URL}wp-json/wp/v2/event'
SOURCE = 'Fort Collins Symphony'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'([A-Za-z]+\s+\d{1,2})[,.]?\s+(\d{4})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'@?\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', re.IGNORECASE)
CITY_RE = re.compile(r'^(.+?),\s*[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?$', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(f'{match.group(1)} {match.group(2)}', '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def parse_times(value):
    times = []
    for match in TIME_RE.finditer(clean_text(value)):
        parsed = parse_time(match.group(0))
        if parsed and parsed not in times:
            times.append(parsed)
    return times or [None]


def labelled_value(soup, label):
    heading = soup.find(
        ['h2', 'h3', 'h4', 'h5', 'h6'],
        string=lambda value: value and clean_text(value).lower() == label.lower(),
    )
    if not heading:
        return ''
    container = heading.parent
    parts = []
    for node in container.find_all(['p', 'div'], recursive=False):
        value = clean_text(node.get_text('\n', strip=True))
        if value and value.lower() != label.lower():
            parts.append(value)
    return '\n'.join(parts)


def parse_location(value):
    lines = [clean_text(line) for line in clean_text(value).splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return None, None

    venue = lines[0]
    city = None
    for line in lines[1:]:
        match = CITY_RE.match(line)
        if match:
            city = clean_text(match.group(1))
            break
    if not city and venue.lower().startswith('fort collins '):
        city = 'Fort Collins'
    return venue or None, city


def fetch_event_links(session):
    links = []
    page = 1
    while True:
        response = session.get(
            EVENT_API_URL,
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'date',
                'order': 'desc',
                '_fields': 'link,title',
            },
            timeout=45,
        )
        response.raise_for_status()
        items = response.json()
        for item in items:
            url = item.get('link')
            title = clean_text((item.get('title') or {}).get('rendered'))
            if url and title:
                links.append((url, title))

        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return links


def parse_event_page(url, api_title, html_text):
    soup = BeautifulSoup(html_text, 'html.parser')
    main = soup.find('main')
    if not main:
        return None

    when = labelled_value(main, 'When')
    where = labelled_value(main, 'Where')
    event_date = parse_date(when)
    venue, city = parse_location(where)
    if not event_date or not venue or not city:
        log_message(
            'Skipping event with incomplete occurrence details',
            event='crawler_event_skipped',
            level='warning',
            url=url,
            has_date=bool(event_date),
            has_venue=bool(venue),
            has_city=bool(city),
        )
        return None

    description = clean_text(main.get_text('\n', strip=True)) or None
    return [
        {
            'title': api_title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for time_from in parse_times(when)
    ]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url, title in fetch_event_links(session):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            page_records = parse_event_page(url, title, response.text)
            if page_records:
                records.extend(page_records)
        except requests.RequestException as error:
            log_message(
                'Unable to fetch event detail',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No parseable concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENT_API_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class FcSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fcsymphony_org',
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
    FcSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
