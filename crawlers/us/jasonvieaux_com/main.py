import re
from datetime import datetime
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://jasonvieaux.com/'
TOUR_URL = urljoin(SOURCE_URL, 'tour/')
SOURCE = 'Jason Vieaux'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

CANADIAN_PROVINCES = {
    'AB', 'BC', 'MB', 'NB', 'NL', 'NS', 'NT', 'NU', 'ON', 'PE', 'QC', 'SK', 'YT'
}


def clean_text(element):
    if element is None:
        return ''
    return re.sub(r'\s+', ' ', element.get_text(' ', strip=True)).strip()


def parse_datetime(value):
    try:
        parsed = datetime.strptime(value.strip(), '%B %d, %Y @ %I:%M %p')
    except ValueError:
        return None
    # Midnight is used by this calendar for undated residencies and should not
    # be presented as a confidently advertised performance time.
    time_from = None if parsed.strftime('%H:%M') == '00:00' else parsed.strftime('%H:%M')
    return parsed.date().isoformat(), time_from


def parse_location(value):
    parts = [part.strip() for part in value.split(',') if part.strip()]
    if len(parts) < 2:
        return None

    region = parts[-1].upper()
    if region in CANADIAN_PROVINCES:
        country_code = 'CA'
        city_index = -2
    elif re.fullmatch(r'[A-Z]{2}', region):
        country_code = 'US'
        city_index = -2
    else:
        # A few US listings omit the state, for example "Alice Tully Hall,
        # New York City".
        country_code = 'US'
        city_index = -1

    city = parts[city_index]
    venue_parts = parts[:city_index]
    venue = ', '.join(venue_parts).strip()
    if not venue or venue.casefold() in {'tbc', 'tba', 'to be confirmed'}:
        return None
    return venue, city, country_code


def normalize_event_url(href):
    if not href:
        return TOUR_URL
    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    if 'returnUrl' in query:
        return unquote(query['returnUrl'][0])
    return urljoin(TOUR_URL, href)


def parse_row(row):
    cells = row.find_all('td', recursive=False)
    if len(cells) < 4:
        return None

    parsed_datetime = parse_datetime(clean_text(cells[0]))
    location = parse_location(clean_text(cells[3]))
    title = clean_text(cells[1])
    if not parsed_datetime or not location or not title:
        return None

    event_date, time_from = parsed_datetime
    venue, city, country_code = location
    link = cells[1].find('a', href=True) or row.find('a', href=True)
    description = clean_text(cells[2]) or None

    return {
        'title': title,
        'date': event_date,
        'url': normalize_event_url(link.get('href') if link else None),
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class JasonVieauxComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jasonvieaux_com',
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
        try:
            response = requests.get(TOUR_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Jason Vieaux tour calendar',
                event='crawler_fetch_failed',
                level='error',
                url=TOUR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        calendars = soup.select('.entry-content.tour')
        if not calendars:
            raise ValueError('Could not find the tour calendar')

        records = []
        for calendar in calendars:
            for row in calendar.select('table tr'):
                record = parse_row(row)
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    JasonVieauxComCrawler().run()


if __name__ == '__main__':
    main()
