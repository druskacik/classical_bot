import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ohorkiestra.com/'
CONCERTS_URL = urljoin(SOURCE_URL, 'koncerty')
SOURCE = '{oh!} Orkiestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}

MONTHS = {
    'stycznia': 1,
    'lutego': 2,
    'marca': 3,
    'kwietnia': 4,
    'maja': 5,
    'czerwca': 6,
    'lipca': 7,
    'sierpnia': 8,
    'września': 9,
    'października': 10,
    'listopada': 11,
    'grudnia': 12,
}

# The orchestra tours, so its home city is deliberately not used as a default.
# Add an entry only when the advertised location makes the city unambiguous.
LOCATION_MARKERS = {
    'w warszawie': ('Warszawa', 'PL'),
    'w gostyniu': ('Gostyń', 'PL'),
    'w katowicach': ('Katowice', 'PL'),
    'w krakowie': ('Kraków', 'PL'),
    'w poznaniu': ('Poznań', 'PL'),
    'we wrocławiu': ('Wrocław', 'PL'),
    'w gdańsku': ('Gdańsk', 'PL'),
    'w szczecinie': ('Szczecin', 'PL'),
    'w łodzi': ('Łódź', 'PL'),
    'w lublinie': ('Lublin', 'PL'),
    'w bydgoszczy': ('Bydgoszcz', 'PL'),
    'w toruniu': ('Toruń', 'PL'),
    'w opolu': ('Opole', 'PL'),
    'w rzeszowie': ('Rzeszów', 'PL'),
}


def clean_text(node):
    if not node:
        return ''
    text = node.get_text('\n', strip=True) if hasattr(node, 'get_text') else str(node)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(node):
    values = [clean_text(part).strip('.') for part in node.select('.full_date > *')]
    values = [value for value in values if value and value != '.']
    if len(values) != 3:
        return None
    day_text, month_text, year_text = values
    month = MONTHS.get(month_text.casefold())
    if not month:
        return None
    try:
        return date(int(year_text), month, int(day_text)).isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = re.fullmatch(r'([01]?\d|2[0-3]):([0-5]\d)', text.strip())
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2)}'


def parse_location(venue):
    folded = venue.casefold()
    for marker, location in LOCATION_MARKERS.items():
        if marker in folded:
            return location
    return None


def parse_detail(soup, url):
    event = soup.select_one('.concerts-container.is-custom2')
    if not event:
        return None

    title = clean_text(event.select_one('h1.heading-concerts'))
    event_date = parse_date(event)
    time_from = parse_time(clean_text(event.select_one('h2.global-italian-text')))
    venue = clean_text(event.select_one('p.paragraph-text-normal:not(.is-custom)'))
    location = parse_location(venue)
    if not title or not event_date or not venue or not location:
        return None

    description_parts = []
    program = clean_text(event.select_one('p.paragraph-text-normal.is-custom'))
    if program:
        description_parts.append(f'Program\n{program}')
    performers = [clean_text(node) for node in event.select('p.concert_paragraph')]
    performers = [performer for performer in performers if performer]
    if performers:
        description_parts.append('Wykonawcy\n' + '\n'.join(performers))

    city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
    }


class OhorkiestraComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ohorkiestra_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(CONCERTS_URL, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        urls = sorted({
            urljoin(SOURCE_URL, link.get('href', '').strip())
            for link in soup.select('a[href*="/concerts/"]')
            if link.get('href', '').strip()
        })
        records = []
        for url in urls:
            try:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
                record = parse_detail(BeautifulSoup(detail_response.text, 'html.parser'), url)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete {oh!} Orkiestra event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, venue, city, or URL is missing',
                    )
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape {oh!} Orkiestra event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['url']),
        )


def main():
    OhorkiestraComCrawler().run()


if __name__ == '__main__':
    main()
