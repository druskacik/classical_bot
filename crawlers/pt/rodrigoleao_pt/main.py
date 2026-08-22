import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.rodrigoleao.pt/'
AGENDA_URL = 'https://www.rodrigoleao.pt/agenda/'
SOURCE = 'Rodrigo Leão'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-PT,pt;q=0.9,en;q=0.7',
}

MONTHS = {
    'jan': 1,
    'fev': 2,
    'mar': 3,
    'abr': 4,
    'mai': 5,
    'jun': 6,
    'jul': 7,
    'ago': 8,
    'set': 9,
    'out': 10,
    'nov': 11,
    'dez': 12,
}

COUNTRY_CODES = {
    'pt': 'PT',
    'sp': 'ES',  # The site uses "Sp" for Spain.
    'es': 'ES',
}


def clean_text(element):
    if element is None:
        return ''
    return re.sub(r'\s+', ' ', element.get_text(' ', strip=True)).strip()


def parse_date(value):
    match = re.search(r'(\d{1,2})\s+([A-Za-zÀ-ÿ]{3})\s*\|\s*(20\d{2})', value)
    if not match:
        return None
    month = MONTHS.get(match.group(2).lower())
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_location(value, url):
    normalized = re.sub(r'\s+', ' ', value).strip(' ,')

    # One current row has its title in the location field. Its first-party
    # ticket URL nevertheless identifies both Casa da Cultura and Ílhavo.
    if 'casa_da_cultura_de_ilhavo' in url:
        return 'Casa da Cultura de Ílhavo', 'Ílhavo', 'PT'

    if normalized == 'Festival Literário de Macau, Grande Auditório do Centro Cultural':
        return 'Grande Auditório do Centro Cultural de Macau', 'Macau', 'MO'

    parts = [part.strip() for part in normalized.split(',') if part.strip()]
    if len(parts) < 2:
        return None
    country_code = COUNTRY_CODES.get(parts[-1].lower())
    if country_code is None:
        return None

    if normalized == 'Teatro Municipal Bragança, PT':
        return 'Teatro Municipal de Bragança', 'Bragança', country_code

    if len(parts) < 3:
        return None
    city = parts[-2]
    venue_parts = parts[:-2]
    # Misty Fest is a presenting festival, while Casa da Música is the venue.
    if venue_parts and venue_parts[0].lower().endswith('fest'):
        venue_parts = venue_parts[1:]
    venue = ', '.join(venue_parts)
    if not venue:
        return None

    corrections = {
        'Ponta Pelgada': 'Ponta Delgada',
        'CColiseu Micaelense': 'Coliseu Micaelense',
    }
    return corrections.get(venue, venue), corrections.get(city, city), country_code


def parse_event(row):
    event_date = parse_date(clean_text(row.select_one('.date')))
    title_element = row.select_one('.title p')
    location_element = row.select_one('.title span')
    title = clean_text(title_element)
    location_text = clean_text(location_element)
    link = row.select_one('.link a[href]')
    url = link.get('href', '').strip() if link else AGENDA_URL

    if not title and 'casa_da_cultura_de_ilhavo' in url:
        title = location_text
    location = parse_location(location_text, url)
    if not title or not event_date or not url or location is None:
        return None

    venue, city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class RodrigoLeaoPtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='rodrigoleao_pt',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PT',
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
            response = requests.get(AGENDA_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Rodrigo Leão agenda',
                event='crawler_fetch_failed',
                level='error',
                url=AGENDA_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for row in soup.select('li.row'):
            if row.select_one('.date') is None or row.select_one('.title') is None:
                continue
            record = parse_event(row)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    RodrigoLeaoPtCrawler().run()


if __name__ == '__main__':
    main()
