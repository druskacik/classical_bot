import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.nataliedraper.net/'
EVENTS_URL = 'https://www.nataliedraper.net/upcoming-performances.html'
SOURCE = 'Natalie Draper'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    name: number
    for number, name in enumerate(
        ('', 'january', 'february', 'march', 'april', 'may', 'june',
         'july', 'august', 'september', 'october', 'november', 'december')
    )
    if name
}

US_REGIONS = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'D.C.', 'FL', 'GA',
    'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA',
    'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY',
    'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX',
    'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY',
}


def clean_text(element):
    text = element.get_text(' ', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_dates(text):
    match = re.match(
        r'^([A-Z]+)\s+(\d{1,2})(?:\s+AND\s+(\d{1,2}))?,\s*(20\d{2})\s*-\s*',
        text,
        re.IGNORECASE,
    )
    if not match:
        return [], text
    month = MONTHS.get(match.group(1).lower())
    if not month:
        return [], text

    parsed = []
    for raw_day in (match.group(2), match.group(3)):
        if not raw_day:
            continue
        try:
            parsed.append(date(int(match.group(4)), month, int(raw_day)).isoformat())
        except ValueError:
            return [], text
    return parsed, text[match.end():].strip()


def parse_location(parts):
    """Return venue, city, country for the locations used in the archive."""
    if len(parts) < 3:
        return None

    location = parts[-1].strip()
    venue = parts[-2].strip()
    us_match = re.match(r'^(.+?),\s*([A-Z]{2}|D\.C\.)$', location)
    if us_match and us_match.group(2) in US_REGIONS:
        city = us_match.group(1).strip()
        # A few entries combine the venue and location in their final field.
        if ',' in city:
            combined = [item.strip() for item in city.split(',') if item.strip()]
            if len(combined) >= 2:
                venue = combined[-2]
                city = combined[-1]
        elif len(parts) < 4:
            # With only a work, performer, and city there is no stated venue.
            return None
        return (venue, city, 'US') if venue and city else None

    country_map = {
        'the netherlands': 'NL',
        'netherlands': 'NL',
        'united kingdom': 'GB',
        'scotland': 'GB',
        'england': 'GB',
        'sweden': 'SE',
    }
    foreign_bits = [bit.strip() for bit in location.split(',') if bit.strip()]
    if len(foreign_bits) >= 2:
        country_code = country_map.get(foreign_bits[-1].lower())
        if country_code:
            city = foreign_bits[0]
            return (venue, city, country_code) if venue and city else None

    country_code = country_map.get(location.lower())
    if not country_code or len(parts) < 4:
        return None

    city = parts[-2].strip()
    venue = parts[-3].strip()
    # Scotland/England appears between the city and United Kingdom.
    if country_code == 'GB' and city.lower() in {'scotland', 'england'}:
        if len(parts) < 5:
            return None
        city = parts[-3].strip()
        venue = parts[-4].strip()
    return (venue, city, country_code) if venue and city else None


def parse_item(item):
    text = clean_text(item)
    dates, details = parse_dates(text)
    if not dates:
        return []

    parts = [part.strip() for part in re.split(r'\s*-\s*', details) if part.strip()]
    if len(parts) < 3:
        return []
    location = parse_location(parts)
    if location is None:
        return []

    title = parts[0].strip()
    venue, city, country_code = location
    if not title:
        return []

    return [
        {
            'title': title,
            'date': event_date,
            'url': EVENTS_URL,
            'time_from': None,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': details,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
    ]


class NatalieDraperNetCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nataliedraper_net',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'venue', 'city'],
    )

    def scrape(self):
        try:
            response = requests.get(EVENTS_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Natalie Draper performances',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for item in soup.select('#wsite-content li'):
            records.extend(parse_item(item))

        return sorted(
            records,
            key=lambda record: (record['date'], record['title'], record['venue']),
        )


def main():
    NatalieDraperNetCrawler().run()


if __name__ == '__main__':
    main()
