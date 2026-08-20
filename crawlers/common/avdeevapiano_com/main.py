import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.avdeevapiano.com/'
SOURCE = 'Yulianna Avdeeva'
CALENDAR_URLS = (
    'https://www.avdeevapiano.com/calendar/',
    'https://www.avdeevapiano.com/past-performances/',
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

# The calendar mixes ISO alpha-2 codes, alpha-3 codes, country names, and the
# non-ISO abbreviation WAL. Normalize every observed form to alpha-2.
COUNTRY_CODES = {
    'AT': 'AT', 'AUT': 'AT', 'BE': 'BE', 'BEL': 'BE', 'CA': 'CA', 'CAN': 'CA',
    'CH': 'CH', 'CHE': 'CH', 'CHINA': 'CN', 'CN': 'CN', 'CRO': 'HR', 'HR': 'HR',
    'CZ': 'CZ', 'CZE': 'CZ', 'DE': 'DE', 'GER': 'DE', 'DEN': 'DK', 'ES': 'ES',
    'ESP': 'ES', 'FIN': 'FI', 'FR': 'FR', 'FRA': 'FR', 'GRC': 'GR', 'ISR': 'IL',
    'IT': 'IT', 'ITA': 'IT', 'JAP': 'JP', 'JP': 'JP', 'JPN': 'JP', 'KOR': 'KR',
    'KR': 'KR', 'LIE': 'LI', 'LT': 'LT', 'LU': 'LU', 'NL': 'NL', 'NLD': 'NL',
    'NOR': 'NO', 'PL': 'PL', 'POL': 'PL', 'POR': 'PT', 'SE': 'SE', 'TW': 'TW',
    'TWN': 'TW', 'UK': 'GB', 'US': 'US', 'USA': 'US', 'WAL': 'GB',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_dates(value, year_hint=None):
    """Expand the site's single dates and its comma/ampersand date groups."""
    value = value.replace('.', ' ').replace('Feburary', 'February')
    year_match = re.search(r'\b(20\d{2})\s*$', value.strip())
    if not year_match and year_hint is None:
        return []
    year = int(year_match.group(1)) if year_match else year_hint
    date_text = value[:year_match.start()] if year_match else value
    parts = re.split(r'\s*(?:,|&)\s*', date_text.strip())
    parsed = []
    current_month = None
    for part in reversed(parts):
        match = re.fullmatch(r'(\d{1,2})(?:\s+([A-Za-z]+))?', part.strip())
        if not match:
            return []
        if match.group(2):
            current_month = MONTHS.get(match.group(2).lower())
        if current_month is None:
            return []
        try:
            parsed.append(date(year, current_month, int(match.group(1))).isoformat())
        except ValueError:
            return []
    return list(reversed(parsed))


def parse_location(value):
    if ',' not in value:
        return None
    city, raw_country = value.rsplit(',', 1)
    city = city.strip()
    country_code = COUNTRY_CODES.get(raw_country.strip().upper())
    if not city or not country_code:
        return None

    # This long-standing calendar typo assigns the Polish spa town Duszniki
    # Zdroj to CZ on one listing. The city is unambiguously in Poland.
    if re.sub(r'[^a-z]', '', city.lower()) == 'dusznikizdroj':
        country_code = 'PL'
    return city, country_code


def parse_event(item, year_hint=None):
    title = clean_text(item.select_one('.perform-title'))
    venue = clean_text(item.select_one('.upcoming-venue'))
    location = parse_location(clean_text(item.select_one('.city')))
    link = item.select_one('a.ticket-link[href]')
    event_dates = parse_dates(clean_text(item.select_one('.date-title')), year_hint)
    if not title or not venue or not location or not link or not event_dates:
        return []

    url = link.get('href', '').strip()
    if not url.startswith(('http://', 'https://')):
        return []
    city, country_code = location
    description = clean_text(item.select_one('.notes')) or None
    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for event_date in event_dates]


class AvdeevaPianoComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='avdeevapiano_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for page_url in CALENDAR_URLS:
            try:
                response = session.get(page_url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Yulianna Avdeeva calendar',
                    event='crawler_fetch_failed',
                    level='error',
                    url=page_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            soup = BeautifulSoup(response.text, 'html.parser')
            year_hint = None
            for item in soup.select('li.event-item'):
                date_text = clean_text(item.select_one('.date-title'))
                explicit_year = re.search(r'\b(20\d{2})\b', date_text)
                if explicit_year:
                    year_hint = int(explicit_year.group(1))
                parsed = parse_event(item, year_hint)
                if not parsed:
                    log_message(
                        'Skipped incomplete Yulianna Avdeeva calendar entry',
                        event='crawler_item_skipped',
                        level='warning',
                        url=page_url,
                    )
                records.extend(parsed)

        unique = {
            (record['title'], record['date'], record['venue'], record['city']): record
            for record in records
        }
        return sorted(
            unique.values(),
            key=lambda record: (record['date'], record['title'], record['venue']),
        )


def main():
    AvdeevaPianoComCrawler().run()


if __name__ == '__main__':
    main()
