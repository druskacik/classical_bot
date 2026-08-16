import re
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://flco.org/'
SOURCE = 'Florida Chamber Orchestra'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'([A-Za-z]+\s+\d{1,2},\s+\d{4})\s*[\u2013\u2014-]\s*'
    r'(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?))',
    re.IGNORECASE,
)
ADDRESS_CITY_RE = re.compile(
    r',\s*([A-Za-z][A-Za-z .\'-]+?)(?:,?\s+FL)(?:\s+\d{5}(?:-\d{4})?)?\s*$',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        return datetime.strptime(value, '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    normalized = value.replace('.', '').upper().strip()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def canonical_page_url(value):
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def card_location(lines, last_date_index):
    for index in range(len(lines) - 1, last_date_index, -1):
        match = ADDRESS_CITY_RE.search(lines[index])
        if not match:
            continue
        city = clean_text(match.group(1))
        if index == 0:
            continue
        venue = clean_text(lines[index - 1])
        if not venue or DATE_TIME_RE.search(venue) or venue.lower().startswith(('ticket', 'call')):
            continue
        return venue, city
    return None, None


def parse_card(card, page_url):
    text = clean_text(card)
    lines = [line for line in text.splitlines() if line]
    matches = list(DATE_TIME_RE.finditer(text))
    if not lines or not matches:
        return []

    title = lines[0]
    last_date_index = max(
        index for index, line in enumerate(lines) if DATE_TIME_RE.search(line)
    )
    venue, city = card_location(lines, last_date_index)
    if not title or not venue or not city:
        return []

    records = []
    for match in matches:
        event_date = parse_date(match.group(1))
        if not event_date:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': canonical_page_url(page_url),
            'time_from': parse_time(match.group(2)),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': text,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def fetch_concert_pages(session):
    page_number = 1
    pages = []
    while True:
        response = session.get(
            API_URL,
            params={
                'search': 'concert',
                'per_page': 100,
                'page': page_number,
                '_fields': 'id,slug,link,title,content',
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        for page in payload:
            title = clean_text(page.get('title', {}).get('rendered'))
            content = page.get('content', {}).get('rendered', '')
            if title.lower() == 'concerts' and DATE_TIME_RE.search(clean_text(content)):
                pages.append(page)

        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page_number >= total_pages:
            return pages
        page_number += 1


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []

    for page in fetch_concert_pages(session):
        soup = BeautifulSoup(page['content']['rendered'], 'html.parser')
        for card in soup.select('.kt-inside-inner-col'):
            records.extend(parse_card(card, page['link']))

    unique = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        # The API currently returns the canonical page before its public legacy
        # copy, so retain the canonical URL when both contain the same event.
        unique.setdefault(key, record)

    result = sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))
    if not result:
        log_message(
            'No dated concert cards found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return result


class FlcoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='flco_org',
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
    FlcoOrgCrawler().run()


if __name__ == '__main__':
    main()
