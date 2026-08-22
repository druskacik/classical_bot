import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.melvyntan.com/'
CONCERTS_URL = f'{SOURCE_URL}concerts/'
SOURCE = 'Melvyn Tan'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

# This is a touring artist's calendar. Locations are resolved only where the
# archive gives a uniquely identifiable venue or an explicit town.
LOCATIONS = {
    'charleston farmhouse, east sussex': ('Charleston Farmhouse', 'Firle', 'GB'),
    'alfriston church east sussex': ('St Andrew\'s Church', 'Alfriston', 'GB'),
    'ightam mote, kent': ('Ightham Mote', 'Ightham', 'GB'),
    'theatr clwyd wales': ('Theatr Clwyd', 'Mold', 'GB'),
    'the britten studio snape': ('The Britten Studio', 'Snape', 'GB'),
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def direct_text(element):
    if element is None:
        return ''
    value = ' '.join(str(node) for node in element.find_all(string=True, recursive=False))
    return re.sub(r'\s+', ' ', value).strip()


def parse_date(value):
    # Ranges on this site describe holidays or multi-day stays and do not give
    # the dates of their individual public performances.
    if 'through' in value.lower():
        return None
    match = re.fullmatch(r'\s*(\d{2})/(\d{2})/(\d{4})\s*', value)
    if not match:
        return None
    try:
        return date(int(match.group(3)), int(match.group(2)), int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_row(row):
    sections = [section for section in row.select('td > div') if 'line' not in section.get('class', [])]
    if len(sections) < 2:
        return None

    event_date = parse_date(clean_text(sections[0].select_one('h3')))
    location_text = direct_text(sections[1])
    location = LOCATIONS.get(location_text.lower())
    if not event_date or not location:
        return None

    venue, city, country_code = location
    description_parts = []
    for section in sections[1:]:
        for unwanted in section.select('.iframe-box'):
            unwanted.decompose()
        value = clean_text(section)
        if value and value not in description_parts:
            description_parts.append(value)

    return {
        'title': f'Melvyn Tan at {venue}',
        'date': event_date,
        'url': CONCERTS_URL,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
    }


class MelvyntanComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='melvyntan_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        try:
            response = requests.get(CONCERTS_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Melvyn Tan concerts',
                event='crawler_fetch_failed',
                level='error',
                url=CONCERTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for row in soup.select('tr.concerts-text'):
            record = parse_row(row)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (record['date'], record['title'], record['venue']),
        )


def main():
    MelvyntanComCrawler().run()


if __name__ == '__main__':
    main()
