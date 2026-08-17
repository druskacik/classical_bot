import re
from datetime import datetime
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.alpharettasymphony.org/'
EVENTS_API_URL = urljoin(SOURCE_URL, 'events?format=json')
SOURCE = 'Alpharetta Symphony'
COUNTRY_CODE = 'US'
TIMEZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_LINK_RE = re.compile(r'\|\s*\d{1,2}\.\d{1,2}\.\d{2,4}\b')
PERFORMANCE_RE = re.compile(
    r'(?P<date>(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'\d{1,2},\s*\d{4})\s+at\s+'
    r'(?P<time>\d{1,2}(?::\d{2})?\s*[ap]m)'
    r'(?:\s+and\s+(?P<time_two>\d{1,2}(?::\d{2})?\s*[ap]m))?',
    re.IGNORECASE,
)
CITY_RE = re.compile(r'\b([A-Za-z][A-Za-z .\'-]+),\s*GA,?\s+\d{5}\b')


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    lines = [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def parse_time(value):
    compact = re.sub(r'\s+', '', value).upper()
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(compact, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def current_event_urls(session):
    response = session.get(SOURCE_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    host = urlparse(SOURCE_URL).netloc
    urls = set()
    for link in soup.select('a[href]'):
        if not DATE_LINK_RE.search(clean_text(link)):
            continue
        url = urljoin(SOURCE_URL, link.get('href')).split('#', 1)[0]
        if urlparse(url).netloc == host:
            urls.add(url)
    return sorted(urls)


def parse_current_event(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    main = soup.select_one('main')
    if not main:
        return []

    heading = main.select_one('h1, h2, h3, h4')
    title = clean_text(heading)
    text = clean_text(main)
    performances = list(PERFORMANCE_RE.finditer(text))
    if not title or not performances:
        return []

    # On these concert templates the venue is the first line after the final
    # date/time line. Keeping this inference local avoids applying the home hall
    # to a future touring page.
    remainder = text[performances[-1].end():].lstrip('\n ')
    venue = remainder.split('\n', 1)[0].strip()
    city_match = CITY_RE.search(text)
    city = city_match.group(1).strip() if city_match else ''
    if not venue or not city:
        return []
    venue = re.sub(rf',\s*{re.escape(city)}$', '', venue, flags=re.IGNORECASE).strip()

    records = []
    for performance in performances:
        try:
            event_date = datetime.strptime(
                performance.group('date'), '%A, %B %d, %Y'
            ).date().isoformat()
        except ValueError:
            continue
        for event_time in (performance.group('time'), performance.group('time_two')):
            if not event_time:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': parse_time(event_time),
                'venue': venue,
                'city': city,
                'country_code': COUNTRY_CODE,
                'description': text,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def legacy_records(session):
    response = session.get(EVENTS_API_URL, timeout=45)
    response.raise_for_status()
    payload = response.json()
    records = []
    for item in payload.get('upcoming', []) + payload.get('past', []):
        location = item.get('location') or {}
        venue = clean_text(location.get('addressTitle'))
        address = clean_text(location.get('addressLine2'))
        city_match = CITY_RE.search(address)
        start_ms = item.get('startDate')
        title = clean_text(item.get('title'))
        if not title or not start_ms or not venue or not city_match:
            continue
        start = datetime.fromtimestamp(start_ms / 1000, tz=TIMEZONE)
        url = urljoin(SOURCE_URL, item.get('fullUrl') or '')
        records.append({
            'title': title,
            'date': start.date().isoformat(),
            'url': url,
            'time_from': start.strftime('%H:%M'),
            'venue': venue,
            'city': city_match.group(1).strip(),
            'country_code': COUNTRY_CODE,
            'description': clean_text(item.get('body') or item.get('excerpt')) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    urls = current_event_urls(session)
    records = legacy_records(session)
    for url in urls:
        try:
            records.extend(parse_current_event(session, url))
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class AlpharettaSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='alpharettasymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
    AlpharettaSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
