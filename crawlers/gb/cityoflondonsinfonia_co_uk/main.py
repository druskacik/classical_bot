import re
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cityoflondonsinfonia.co.uk/'
SOURCE = 'City of London Sinfonia'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/events'
COUNTRY_CODE = 'GB'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(' ', strip=True)
    else:
        value = unescape(str(value))
    return re.sub(r'\s+', ' ', value.replace('\xa0', ' ')).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def event_pages(session):
    """Enumerate every current event via the first-party WordPress API."""
    page = 1
    events = []
    while True:
        response = get_json(
            session,
            API_URL,
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'id',
                'order': 'asc',
                '_fields': 'link,title',
            },
        )
        batch = response.json()
        if not isinstance(batch, list):
            raise ValueError('Unexpected events API response')
        events.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return events


def parse_heading_year(soup):
    heading = soup.select_one('.event_heading')
    if not heading:
        return None
    match = re.search(r'\b(20\d{2})\b', clean_text(heading))
    return int(match.group(1)) if match else None


def parse_show_row(text, year):
    match = re.fullmatch(
        r'(\d{1,2}\s+[A-Za-z]+)\s+'
        r'(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*-\s*(.+?)(?:\s*-)?\s*Book',
        clean_text(text),
        re.I,
    )
    if not match or year is None:
        return None
    try:
        date = datetime.strptime(f'{match.group(1)} {year}', '%d %b %Y').date()
        time_from = datetime.strptime(
            re.sub(r'\s+', '', match.group(2)).upper(), '%I:%M%p'
            if ':' in match.group(2) else '%I%p',
        ).strftime('%H:%M')
    except ValueError:
        return None
    venue = match.group(3).strip(' -')
    if not venue:
        return None
    return date.isoformat(), time_from, venue


def parse_event(session, item):
    url = clean_text(item.get('link'))
    if not url:
        return []
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    title = clean_text(soup.select_one('.event_heading h1'))
    year = parse_heading_year(soup)
    description_node = soup.select_one('main .show_info > .text_col')
    description = clean_text(description_node) or None
    if not title:
        return []

    records = []
    for row in soup.select('main .show_details_table .show_row'):
        occurrence = parse_show_row(row, year)
        if not occurrence:
            continue
        date, time_from, venue = occurrence
        # The current calendar's selectable locations are all London venues.
        # This is applied only to rows on that calendar, not to arbitrary tour pages.
        records.append({
            'title': title,
            'date': date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': 'London',
            'country_code': COUNTRY_CODE,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class CityOfLondonSinfoniaCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cityoflondonsinfonia_co_uk',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for item in event_pages(session):
            try:
                records.extend(parse_event(session, item))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=item.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    CityOfLondonSinfoniaCoUkCrawler().run()


if __name__ == '__main__':
    main()
