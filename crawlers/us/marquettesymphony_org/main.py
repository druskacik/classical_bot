import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.marquettesymphony.org/'
SOURCE = 'Marquette Symphony Orchestra'
CITY = 'Marquette'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

CALENDAR_PATH_RE = re.compile(r'^/\d{4}-\d{2}-calendar/?$')
DATE_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2}),\s+(\d{4})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP])\.?M\.?\b', re.IGNORECASE)
VENUE_RE = re.compile(r'(?:^|[,;]\s*|\s)at\s+(?:the\s+)?([^\n]+)', re.IGNORECASE)


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(match):
    try:
        return datetime.strptime(' '.join(match.groups()), '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(match):
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        return None
    if match.group(3).upper() == 'P' and hour != 12:
        hour += 12
    elif match.group(3).upper() == 'A' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def clean_venue(value):
    venue = clean_text(value).strip(' ,.;')
    venue = re.sub(r'\s+on NMU(?:\'s)? Campus$', '', venue, flags=re.IGNORECASE)
    venue = re.sub(r',\s*Presque Isle$', '', venue, flags=re.IGNORECASE)
    return venue.strip(' ,.;')


def title_from_text(text, first_date_start):
    prefix = clean_text(text[:first_date_start])
    lines = [line for line in prefix.splitlines() if line]
    return clean_text(' '.join(lines))


def records_from_text(text, page_url, fallback_title=None):
    text = clean_text(text)
    matches = list(DATE_RE.finditer(text))
    if not matches:
        return []

    title = title_from_text(text, matches[0].start())
    if fallback_title and re.fullmatch(r'(?:and\s+)?more!*', title, re.IGNORECASE):
        title = fallback_title
    if not title:
        return []

    records = []
    for index, date_match in enumerate(matches):
        preceding = text[max(0, date_match.start() - 20):date_match.start()].lower()
        if 'raindate' in preceding or 'rain date' in preceding:
            continue

        event_date = parse_date(date_match)
        chunk_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        chunk = text[date_match.end():chunk_end]
        time_match = TIME_RE.search(chunk)
        if not event_date or not time_match:
            continue

        location_text = chunk[time_match.end():]
        venue_match = VENUE_RE.search(location_text)
        if not venue_match:
            continue
        venue = clean_venue(venue_match.group(1).split('\n', 1)[0])
        if not venue or venue.lower() in {'same location', CITY.lower()}:
            continue

        records.append({
            'title': title,
            'date': event_date,
            'url': page_url,
            'time_from': parse_time(time_match),
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': text,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def records_from_block(block, page_url):
    paragraphs = block.find_all('p', recursive=False)
    dated_indexes = [
        index for index, paragraph in enumerate(paragraphs)
        if DATE_RE.search(clean_text(paragraph.get_text('\n', strip=True)))
    ]
    if len(dated_indexes) <= 1:
        return records_from_text(block.get_text('\n', strip=True), page_url)

    records = []
    fallback_title = None
    for position, start in enumerate(dated_indexes):
        if position == 0:
            start = 0
        end = dated_indexes[position + 1] if position + 1 < len(dated_indexes) else len(paragraphs)
        text = '\n'.join(paragraph.get_text('\n', strip=True) for paragraph in paragraphs[start:end])
        parsed = records_from_text(text, page_url, fallback_title=fallback_title)
        if parsed and fallback_title is None:
            fallback_title = parsed[0]['title']
        records.extend(parsed)
    return records


def calendar_urls(session):
    response = session.get(SOURCE_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    urls = set()
    for link in soup.select('a[href]'):
        url = urljoin(SOURCE_URL, link['href'])
        if CALENDAR_PATH_RE.fullmatch(urlparse(url).path):
            urls.add(url.rstrip('/'))
    return sorted(urls, reverse=True)


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    urls = calendar_urls(session)
    records = []

    for url in urls:
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Concert calendar request failed',
                event='crawler_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue

        soup = BeautifulSoup(response.text, 'html.parser')
        for block in soup.select('main .sqs-block-html .sqs-html-content'):
            records.extend(records_from_block(block, url))

    unique = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        unique.setdefault(key, record)

    result = sorted(unique.values(), key=lambda item: (item['date'], item['time_from'], item['title']))
    if not result:
        log_message(
            'No concerts found on published calendar pages',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return result


class MarquetteSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='marquettesymphony_org',
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
    MarquetteSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
