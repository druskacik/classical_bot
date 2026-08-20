import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.claricejensen.com/'
EVENTS_URL = urljoin(SOURCE_URL, 'events/')
SOURCE = 'Clarice Jensen'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_CODES = {
    'argentina': 'AR',
    'brazil': 'BR',
    'chile': 'CL',
    'colombia': 'CO',
    'italy': 'IT',
    'malaysia': 'MY',
    'mexico': 'MX',
    'peru': 'PE',
    'philippines': 'PH',
    'republic of korea': 'KR',
    'singapore': 'SG',
    'south korea': 'KR',
    'spain': 'ES',
    'thailand': 'TH',
    'uk': 'GB',
    'united kingdom': 'GB',
    'us': 'US',
    'usa': 'US',
}

US_REGIONS = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID',
    'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS',
    'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND', 'OH', 'OK',
    'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV',
    'WI', 'WY', 'DC',
}

OCCURRENCE_RE = re.compile(
    r'(?m)^\s*(\d{2}/\d{2}/\d{4})\s*-\s*(.+?)\s*\|\s*(.+?)\s*$'
)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    for pattern in ('%A, %B %d, %Y', '%B %d, %Y'):
        try:
            return datetime.strptime(value.strip(), pattern).date().isoformat()
        except ValueError:
            continue
    return None


def parse_city_country(value):
    parts = [part.strip() for part in value.rsplit(',', 2) if part.strip()]
    if len(parts) < 2:
        return None

    suffix = parts[-1]
    normalized_suffix = suffix.lower().rstrip('.')
    if normalized_suffix in COUNTRY_CODES:
        city_parts = parts[:-1]
        if (
            COUNTRY_CODES[normalized_suffix] == 'US'
            and len(city_parts) > 1
            and city_parts[-1].upper() in US_REGIONS
        ):
            city_parts = city_parts[:-1]
        return ', '.join(city_parts), COUNTRY_CODES[normalized_suffix]
    if suffix.upper() in US_REGIONS:
        return ', '.join(parts[:-1]), 'US'
    return None


def event_url(event):
    link = event.select_one('.event_info_link a[href]')
    href = link.get('href', '').strip() if link else ''
    return urljoin(SOURCE_URL, href) if href else EVENTS_URL


def make_record(title, event_date, url, venue, location, description):
    parsed_location = parse_city_country(location)
    if not title or not event_date or not venue or not parsed_location:
        return None
    city, country_code = parsed_location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_event(event):
    title = clean_text(event.select_one('.event_title'))
    date_text = clean_text(event.select_one('.event_date'))
    venue = clean_text(event.select_one('.event_venue'))
    city = clean_text(event.select_one('.event_city'))
    description = clean_text(event.select_one('.event_info'))
    url = event_url(event)

    occurrences = OCCURRENCE_RE.findall(description)
    if occurrences:
        records = []
        for raw_date, occurrence_venue, location in occurrences:
            try:
                event_date = datetime.strptime(raw_date, '%m/%d/%Y').date().isoformat()
            except ValueError:
                continue
            record = make_record(
                title, event_date, url, occurrence_venue.strip(), location.strip(), description
            )
            if record:
                records.append(record)
        return records

    event_date = parse_date(date_text)
    record = make_record(title, event_date, url, venue, city, description)
    return [record] if record else []


class ClariceJensenComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='claricejensen_com',
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
        dedupe_subset=['title', 'date', 'venue', 'city'],
    )

    def scrape(self):
        try:
            response = requests.get(EVENTS_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Clarice Jensen events',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for event in soup.select('.event'):
            records.extend(parse_event(event))

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['title'], record['venue'], record['city']
            ),
        )


def main():
    ClariceJensenComCrawler().run()


if __name__ == '__main__':
    main()
