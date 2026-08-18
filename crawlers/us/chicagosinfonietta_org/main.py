import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://chicagosinfonietta.org/'
SOURCE = 'Chicago Sinfonietta'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUE_CITIES = {
    'Harris Theater': 'Chicago',
    'Kenwood United Church of Christ': 'Chicago',
    'Pick-Staiger Concert Hall': 'Evanston',
    'Wentz Concert Hall': 'Naperville',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    value = re.sub(r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+', '', clean_text(value), flags=re.I)
    try:
        return datetime.strptime(value, '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    value = clean_text(value).upper().replace('.', '')
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_venue(value):
    value = clean_text(value)
    match = re.fullmatch(r'(.+?)\s*\(([^()]+)\)', value)
    if match:
        venue = match.group(1).strip()
        location = match.group(2).strip()
        city = 'Chicago' if 'Chicago' in location else location
        return venue, city

    city = VENUE_CITIES.get(value)
    return (value, city) if value and city else (None, None)


def is_nonconcert_occurrence(description, raw_date):
    date_without_year = re.sub(r',?\s+\d{4}$', '', raw_date)
    return bool(re.search(
        rf'\b(?:conversation|panel|reception|gala)\s+on\s+(?:\w+,\s+)?{re.escape(date_without_year)}\b',
        description,
        re.I,
    ))


def records_from_page(page):
    content = page.get('content', {}).get('rendered', '')
    soup = BeautifulSoup(content, 'html.parser')
    description = clean_text(content) or None
    title = clean_text(page.get('title', {}).get('rendered', ''))
    url = page.get('link', '')
    if not title or not url:
        return []

    records = []
    for table in soup.find_all('table'):
        headers = [clean_text(node).lower() for node in table.select('thead th')]
        if headers[:3] != ['date', 'time', 'venue']:
            continue

        schedule_heading = table.find_previous(['h1', 'h2', 'h3'])
        if not schedule_heading or clean_text(schedule_heading) != 'Schedule':
            continue

        for row in table.select('tbody tr'):
            cells = row.find_all(['td', 'th'], recursive=False)
            if len(cells) < 3:
                continue
            raw_date = clean_text(cells[0])
            event_date = parse_date(raw_date)
            time_from = parse_time(cells[1])
            venue, city = parse_venue(cells[2])
            if not event_date or not venue or not city:
                continue
            if description and is_nonconcert_occurrence(description, raw_date):
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
            })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page_number = 1

    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page_number},
            timeout=60,
        )
        response.raise_for_status()
        pages = response.json()
        for page in pages:
            records.extend(records_from_page(page))

        total_pages = int(response.headers.get('X-WP-TotalPages', page_number))
        if page_number >= total_pages:
            break
        page_number += 1

    if not records:
        log_message(
            'No concert schedules found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class ChicagoSinfoniettaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chicagosinfonietta_org',
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
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ChicagoSinfoniettaOrgCrawler().run()


if __name__ == '__main__':
    main()
