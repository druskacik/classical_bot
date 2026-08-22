import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://rikemmett.com/'
ARCHIVE_URL = 'https://rikemmett.com/shows/archives/'
SOURCE = 'Rik Emmett'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-CA,en;q=0.9',
}

COUNTRY_CODES = {
    'Canada': 'CA',
    'United States': 'US',
}


def clean_text(element):
    if element is None:
        return ''
    return re.sub(r'\s+', ' ', element.get_text(' ', strip=True)).strip()


def parse_date(value):
    match = re.search(r'\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+([A-Z][a-z]{2}\s+\d{1,2}\s+\d{4})', value)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%b %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\bTime:\s*(\d{1,2}):([0-5]\d)\s*([ap])m\b', value, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{match.group(2)}'


def parse_event(tbody, title):
    row = tbody.select_one('tr.gigpress-row')
    if row is None:
        return None

    date_element = row.select_one('.gigpress-date')
    detail_link = date_element.select_one('a[href]') if date_element else None
    event_date = parse_date(clean_text(date_element))
    city_element = row.select_one('.gigpress-city')
    city_link = city_element.select_one('a') if city_element else None
    city = clean_text(city_link) if city_link else clean_text(city_element).split(',')[0].strip()
    venue = clean_text(row.select_one('.gigpress-venue'))
    country_code = COUNTRY_CODES.get(clean_text(row.select_one('.gigpress-country')))
    url = detail_link.get('href', '').strip() if detail_link else ''

    # GigPress contains one online-only appearance with placeholder location
    # values. It is not a physical concert occurrence with a real city/venue.
    if city.lower() == 'internet' or venue.lower() == 'online':
        return None

    if not all((title, event_date, url, venue, city, country_code)):
        return None

    info = clean_text(tbody.select_one('tr.gigpress-info'))
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(info),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': info or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class RikemmettComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='rikemmett_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
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
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        try:
            response = requests.get(ARCHIVE_URL, headers=HEADERS, timeout=60)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Rik Emmett show archive',
                event='crawler_fetch_failed',
                level='error',
                url=ARCHIVE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for table in soup.select('table.gigpress-table'):
            heading = table.find_previous('h3')
            title = clean_text(heading)
            title = re.sub(r'(?:\s+(?:RSS|iCalendar))+\s*$', '', title).strip()
            for tbody in table.select('tbody'):
                record = parse_event(tbody, title)
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    RikemmettComCrawler().run()


if __name__ == '__main__':
    main()
