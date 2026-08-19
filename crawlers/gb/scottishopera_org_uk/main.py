import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.scottishopera.org.uk/'
SOURCE = 'Scottish Opera'
API_URL = urljoin(SOURCE_URL, 'umbraco/surface/showsurface/shows')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}
PAGE_SIZE = 100
POSTCODE_RE = re.compile(r'^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$', re.I)
ADMIN_AREAS = {'argyll', 'isle of islay', 'benbecula', 'moray scotland'}
VENUE_CITIES = {'Seil Island Community Hall': 'Seil'}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, page):
    response = session.get(
        API_URL,
        params={
            '_page': page,
            '_limit': PAGE_SIZE,
            'sortBy': 'upcoming',
            'eventType': 'Live Performance',
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def get_all_items(session):
    items = []
    page = 1
    while True:
        batch = get_json(session, page)
        if not batch:
            break
        items.extend(batch)
        total = int(batch[0].get('count') or len(items))
        if len(items) >= total or len(batch) < PAGE_SIZE:
            break
        page += 1
    return items


def parse_event_datetime(value, today=None):
    match = re.fullmatch(
        r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d{1,2}) ([A-Z][a-z]{2,3}), '
        r'(\d{1,2}):(\d{2}) (AM|PM)',
        clean_text(value),
        re.I,
    )
    if not match:
        return None

    weekday, day, month, hour, minute, meridiem = match.groups()
    month = 'Sep' if month.lower() == 'sept' else month.title()
    meridiem = meridiem.upper()
    weekday = weekday.title()
    today = today or date.today()
    for year in range(today.year, today.year + 3):
        try:
            candidate = datetime.strptime(
                f'{day} {month} {year} {hour}:{minute} {meridiem}',
                '%d %b %Y %I:%M %p',
            )
        except ValueError:
            continue
        if candidate.date() >= today and candidate.strftime('%a') == weekday:
            return candidate
    return None


def extract_city(where, address):
    if clean_text(where) in VENUE_CITIES:
        return VENUE_CITIES[clean_text(where)]
    where_parts = [part.strip() for part in clean_text(where).split(',') if part.strip()]
    if len(where_parts) > 1:
        return where_parts[-1]

    parts = [part.strip() for part in clean_text(address).split(',') if part.strip()]
    candidates = [
        part
        for part in parts[1:]
        if not POSTCODE_RE.fullmatch(part) and part.lower() not in ADMIN_AREAS
    ]
    return candidates[-1] if candidates else None


def detail_description(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    sections = []
    for node in soup.select('main .text--wrapper'):
        classes = set(node.get('class') or [])
        if 'collapse-header' in classes:
            continue
        text = clean_text(node.get_text('\n', strip=True))
        if not text or re.search(r'\b\d{1,2}\.\d{2}(?:am|pm)\b', text, re.I):
            continue
        sections.append(text)
    return '\n\n'.join(sections) or None


def listing_record(item):
    if not item.get('live-performance'):
        return None
    event_datetime = parse_event_datetime(item.get('when'))
    title = clean_text(item.get('title'))
    subtitle = clean_text(item.get('pop-up-name'))
    venue = clean_text(item.get('where'))
    city = extract_city(venue, item.get('address'))
    path = item.get('cta-href')
    if not all((event_datetime, title, venue, city, path)):
        return None
    if subtitle and subtitle.lower() not in title.lower():
        title = f'{title} — {subtitle}'
    return {
        'title': title,
        'date': event_datetime.date().isoformat(),
        'url': urljoin(SOURCE_URL, path),
        'time_from': event_datetime.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': clean_text(item.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = [listing_record(item) for item in get_all_items(session)]
    records = [record for record in records if record]

    descriptions = {}
    urls = sorted({record['url'] for record in records})
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(detail_description, session, url): url for url in urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        detail = descriptions.get(record['url'])
        summary = record['description']
        if detail and summary and summary not in detail:
            record['description'] = f'{summary}\n\n{detail}'
        elif detail:
            record['description'] = detail

    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class ScottishOperaOrgUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='scottishopera_org_uk',
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
    ScottishOperaOrgUkCrawler().run()


if __name__ == '__main__':
    main()
