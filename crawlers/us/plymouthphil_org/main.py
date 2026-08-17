import re
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://plymouthphil.org/'
SOURCE = 'Plymouth Philharmonic Orchestra'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|'
    r'Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?' 
    r'\s+\d{1,2},\s+\d{4}\s*\|\s*\d{1,2}(?::\d{2})?\s*[AP]M',
    re.IGNORECASE,
)
CITY_RE = re.compile(r'^([^,\n]+),\s*(?:MA|Massachusetts)(?:\s+\d{5})?$', re.I)


def clean_text(value):
    if not value:
        return ''
    value = str(value)
    text = BeautifulSoup(value, 'html.parser').get_text(' ', strip=True) if '<' in value else unescape(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_datetime(value):
    value = clean_text(value).replace('|', ' ')
    value = re.sub(r'\bSept\.?\b', 'Sep', value, flags=re.I)
    value = re.sub(r'\b([A-Za-z]{3})\.', r'\1', value)
    value = re.sub(r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*', '', value, flags=re.I)
    value = re.sub(r'(?<=\d)(?=[AP]M\b)', ' ', value)
    value = re.sub(r'\s+', ' ', value).strip().upper()
    for pattern in ('%B %d, %Y %I:%M %p', '%B %d, %Y %I %p',
                    '%b %d, %Y %I:%M %p', '%b %d, %Y %I %p'):
        try:
            parsed = datetime.strptime(value, pattern)
            return parsed.date().isoformat(), parsed.strftime('%H:%M')
        except ValueError:
            continue
    return None


def content_lines(html):
    soup = BeautifulSoup(html or '', 'html.parser')
    return [clean_text(part) for part in soup.get_text('\n', strip=True).splitlines() if clean_text(part)]


def extract_title(lines):
    ignored = {'DETAILS', 'BUY TICKETS!', 'PERFORMERS', 'DATE', 'DATES', 'VENUE'}
    for line in lines[:20]:
        if line.upper() not in ignored and not line.upper().startswith(('TICKETS', 'SAT.', 'SUN.')):
            return line
    return ''


def extract_venue(lines):
    marker = next((index for index, line in enumerate(lines) if line.upper() == 'VENUE'), None)
    if marker is None:
        return None

    following = lines[marker + 1:marker + 8]
    city = None
    city_index = None
    for index, line in enumerate(following):
        match = CITY_RE.match(line)
        if match:
            city = clean_text(match.group(1))
            city_index = index
            break
    if city_index is None:
        return None

    venue_parts = [
        line for line in following[:city_index]
        if not re.search(r'\d', line)
        and line != city
        and line.lower().rstrip('!') not in {'new location'}
    ]
    venue = ' '.join(venue_parts)
    return (venue, city) if venue else None


def page_records(page):
    slug = page.get('slug', '').lower()
    if any(word in slug for word in ('sample', 'test', 'template', 'backup')):
        return []
    lines = content_lines(page.get('content', {}).get('rendered'))
    title = extract_title(lines)
    location = extract_venue(lines)
    url = page.get('link', '')
    if not title or not location or not url.startswith(('http://', 'https://')):
        return []

    venue, city = location
    occurrences = []
    for line in lines:
        for match in DATE_RE.finditer(line):
            parsed = parse_datetime(match.group(0))
            if parsed and parsed not in occurrences:
                occurrences.append(parsed)

    if not occurrences:
        return []

    description = '\n'.join(lines)
    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for event_date, event_time in occurrences]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page_number = 1

    while True:
        response = session.get(
            API_URL,
            params={
                'search': 'concert',
                'per_page': 100,
                'page': page_number,
                '_fields': 'content,link,slug,title',
            },
            timeout=60,
        )
        response.raise_for_status()
        pages = response.json()
        for page in pages:
            records.extend(page_records(page))

        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page_number >= total_pages:
            break
        page_number += 1

    if not records:
        log_message(
            'No parseable concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    unique = {
        (item['url'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    return sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class PlymouthPhilOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='plymouthphil_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    PlymouthPhilOrgCrawler().run()


if __name__ == '__main__':
    main()
