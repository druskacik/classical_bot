import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cmnw.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'concerts-events/calendar')
SOURCE = 'Chamber Music Northwest'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\.\s+'
    r'([A-Z][a-z]+\s+\d{1,2},\s+\d{4})'
)
TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?\s*[ap]m)\b', re.IGNORECASE)
CITY_RE = re.compile(r'^(.+?),\s+[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?$', re.MULTILINE)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return ''
    try:
        return datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    normalized = re.sub(r'\s+', ' ', match.group(1).upper())
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def event_metadata(node):
    title_node = node.find('strong')
    link = node.select_one('a[href*="/concerts-events/"]')
    time_node = node.select_one('.nr-tooltip-time')
    if not title_node or not link or not time_node:
        return None

    title = clean_text(title_node.get_text(' ', strip=True))
    event_date = parse_date(time_node.get_text('\n', strip=True))
    url = urljoin(CALENDAR_URL, link.get('href'))
    if not title or title == 'Concerts & Events' or not event_date:
        return None

    lines = [clean_text(line) for line in node.get_text('\n', strip=True).splitlines()]
    lines = [line for line in lines if line]
    city = ''
    city_index = None
    for index, line in enumerate(lines):
        match = CITY_RE.fullmatch(line)
        if match:
            city = clean_text(match.group(1))
            city_index = index
            break

    venue = ''
    if city_index is not None:
        address_start = city_index - 1
        if address_start >= 0 and re.search(r'\d', lines[address_start]):
            address_start -= 1
        if address_start >= 0:
            candidate = lines[address_start]
            if candidate not in {title, 'LEARN MORE', '+ Add to Calendar'} and not DATE_RE.search(candidate):
                venue = candidate

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(time_node.get_text(' ', strip=True)),
        'venue': venue,
        'city': city,
    }


def detail_data(session, url):
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Could not fetch CMNW event detail',
            event='crawler_detail_fetch_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None, '', ''

    soup = BeautifulSoup(response.text, 'html.parser')
    article = soup.select_one('main article')
    if not article:
        return None, '', ''
    for node in article.select('#nr-breadcrumbs, nav, script, style'):
        node.decompose()
    description = clean_text(article.get_text('\n', strip=True)) or None
    lines = description.splitlines() if description else []
    detail_venue = ''
    detail_city = ''
    for index, line in enumerate(lines):
        city_match = CITY_RE.fullmatch(line)
        if city_match:
            detail_city = clean_text(city_match.group(1))
        if re.match(r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),', line):
            if index:
                detail_venue = clean_text(lines[index - 1])
            break
    return description, detail_venue, detail_city


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(CALENDAR_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    records = []
    seen = set()
    for node in soup.select('.nr-calendar-entry-border'):
        metadata = event_metadata(node)
        if not metadata:
            continue
        key = (metadata['url'], metadata['date'], metadata['time_from'])
        if key in seen:
            continue
        seen.add(key)
        description, detail_venue, detail_city = detail_data(session, metadata['url'])
        metadata['venue'] = metadata['venue'] or detail_venue
        metadata['city'] = metadata['city'] or detail_city
        # Detail pages for the Portland campuses occasionally omit their address.
        # The calendar explicitly locates all other out-of-city performances.
        if metadata['venue'] and not metadata['city']:
            metadata['city'] = 'Portland'
        if not metadata['venue'] or not metadata['city']:
            continue
        metadata.update({
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
        records.append(metadata)

    if not records:
        log_message(
            'No valid CMNW calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class CmnwOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cmnw_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    CmnwOrgCrawler().run()


if __name__ == '__main__':
    main()
