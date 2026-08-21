import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://klausmertens.eu/'
SOURCE = 'Klaus Mertens'
EVENTS_URL = urljoin(SOURCE_URL, 'events.php')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

# The artist's calendar uses a mixture of ISO codes and older German vehicle-style
# abbreviations. Locations without one of these explicit suffixes are in Germany.
COUNTRY_SUFFIXES = {
    'A': 'AT',
    'B': 'BE',
    'BEL': 'BE',
    'CH': 'CH',
    'CN': 'CN',
    'CZ': 'CZ',
    'DK': 'DK',
    'E': 'ES',
    'ES': 'ES',
    'F': 'FR',
    'H': 'HU',
    'HU': 'HU',
    'I': 'IT',
    'IL': 'IL',
    'JP': 'JP',
    'LUX': 'LU',
    'NL': 'NL',
    'OMAN': 'OM',
    'P': 'PT',
    'PL': 'PL',
    'PT': 'PT',
    'RO': 'RO',
    'SLO': 'SI',
    'USA': 'US',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_city(value):
    city = clean_text(value)
    match = re.search(r'\s*\(([^()]*)\)\s*$', city)
    if match and match.group(1).strip().upper() in COUNTRY_SUFFIXES:
        suffix = match.group(1).strip().upper()
        return city[:match.start()].strip(), COUNTRY_SUFFIXES[suffix]
    return city, 'DE'


def parse_date(value):
    match = re.search(r'\b(\d{2}\.\d{2}\.\d{4})\b', clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.fullmatch(r'([01]?\d|2[0-3]):([0-5]\d)', clean_text(value))
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def first_text(cell, selectors):
    for selector in selectors:
        value = clean_text(cell.select_one(selector))
        if value:
            return value
    return ''


def parse_row(row, page_url):
    cells = row.find_all('td', recursive=False)
    if len(cells) < 2:
        return None
    facts, details = cells[0], cells[1]
    event_date = parse_date(facts.select_one('.date'))
    city, country_code = parse_city(facts.select_one('.city'))
    venue = clean_text(facts.select_one('.location'))
    title = first_text(
        details,
        ['.title', '.composer', '.subtitle', '.organizer', '.opus'],
    )
    if not all((title, event_date, city, venue)):
        return None

    description_parts = []
    for node in details.select('p'):
        if 'more' in (node.get('class') or []):
            continue
        text = clean_text(node)
        if text and text not in description_parts:
            description_parts.append(text)
    link = details.select_one('a[href]')
    event_url = urljoin(page_url, link['href']) if link else page_url

    return {
        'title': title,
        'date': event_date,
        'url': event_url,
        'time_from': parse_time(facts.select_one('.time')),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class KlausMertensEuCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='klausmertens_eu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(EVENTS_URL, timeout=45)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            years = {
                int(option['value'])
                for option in soup.select('select[name="year"] option[value]')
                if option['value'].isdigit() and int(option['value']) > 0
            }
            if not years:
                raise ValueError('No archive years found in the schedule filter')

            records = []
            skipped = 0
            for year in sorted(years):
                page_url = f'{EVENTS_URL}?year={year}'
                page = session.get(EVENTS_URL, params={'year': year}, timeout=45)
                page.raise_for_status()
                page_soup = BeautifulSoup(page.text, 'html.parser')
                for row in page_soup.select('table tr'):
                    record = parse_row(row, page_url)
                    if record:
                        records.append(record)
                    else:
                        skipped += 1
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Klaus Mertens concert calendar',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        log_message(
            'Parsed Klaus Mertens concert calendar',
            event='crawler_parse_completed',
            url=EVENTS_URL,
            record_count=len(records),
            skipped_count=skipped,
            archive_year_count=len(years),
        )
        return records


def main():
    return KlausMertensEuCrawler().run()


if __name__ == '__main__':
    main()
