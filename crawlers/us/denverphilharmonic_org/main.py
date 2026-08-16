import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://denverphilharmonic.org/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/production'
SOURCE = 'Denver Philharmonic'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_FORMATS = ('%A, %B %d, %Y', '%B %d, %Y')
STATE_CITY_RE = re.compile(r'(?:^|\n)([^\n,]+),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?', re.I)
TIME_RE = re.compile(
    r'(?:concert|performance|show|event|music)\s+(?:begins?\s+)?at\s+'
    r'(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)',
    re.I,
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
    value = clean_text(value)
    value = re.sub(r'(?<=\d)(?:st|nd|rd|th)', '', value, flags=re.I)
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(value.title(), date_format).date().isoformat()
        except ValueError:
            pass
    return None


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    normalized = match.group(1).replace('.', '').upper().replace(' ', '')
    for date_format in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(normalized, date_format).strftime('%H:%M')
        except ValueError:
            pass
    return None


def location_from_paragraph(paragraph):
    text = clean_text(paragraph)
    strong = paragraph.find('strong')
    venue = clean_text(strong) if strong else ''
    address_match = STATE_CITY_RE.search(f'\n{text}')
    if address_match and venue:
        return venue, clean_text(address_match.group(1))
    if venue and re.search(r'\b(?:st(?:reet)?|ave(?:nue)?|broadway|blvd|road|rd)\b', text, re.I):
        return venue, 'Denver'
    return None


def fallback_location(soup):
    for group in soup.select('.sidebar__details--group'):
        if group.select_one('.svg--cal'):
            continue
        paragraphs = group.select('.sidebar__details--copy > p')
        if len(paragraphs) < 2:
            continue
        venue = clean_text(paragraphs[0])
        address = clean_text(paragraphs[1])
        if venue and re.search(r'\d+\s+.+(?:street|st|avenue|ave|broadway|blvd|road|rd)\b', address, re.I):
            city_match = STATE_CITY_RE.search(f'\n{address}')
            return venue, clean_text(city_match.group(1)) if city_match else 'Denver'
    return None


def parse_occurrences(soup):
    default_location = fallback_location(soup)
    occurrences = []
    for group in soup.select('.sidebar__details--group'):
        if not group.select_one('.svg--cal'):
            continue
        paragraphs = group.select('.sidebar__details--copy > p')
        for index, paragraph in enumerate(paragraphs):
            event_date = parse_date(paragraph.get_text(' ', strip=True))
            if not event_date:
                continue
            location = None
            time_from = None
            for following in paragraphs[index + 1:]:
                if parse_date(following.get_text(' ', strip=True)):
                    break
                location = location or location_from_paragraph(following)
                time_from = time_from or parse_time(following.get_text('\n', strip=True))
            location = location or default_location
            if location:
                occurrences.append({
                    'date': event_date,
                    'time_from': time_from,
                    'venue': location[0],
                    'city': location[1],
                })
    return occurrences


def fetch_productions(session):
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page, 'orderby': 'date', 'order': 'desc'},
            timeout=45,
        )
        if response.status_code == 400 and page > 1:
            break
        response.raise_for_status()
        items = response.json()
        if not items:
            break
        yield from items
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []

    for production in fetch_productions(session):
        url = production.get('link', '')
        title = clean_text(production.get('title', {}).get('rendered'))
        if not url or not title:
            continue
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Production page request failed',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue

        soup = BeautifulSoup(response.text, 'html.parser')
        description = clean_text(production.get('content', {}).get('rendered')) or None
        for occurrence in parse_occurrences(soup):
            records.append({
                'title': title,
                **occurrence,
                'url': url,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    if not records:
        log_message(
            'No parseable production occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['title'], item['time_from'] or ''))


class DenverPhilharmonicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='denverphilharmonic_org',
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
    DenverPhilharmonicOrgCrawler().run()


if __name__ == '__main__':
    main()
