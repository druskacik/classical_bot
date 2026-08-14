import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.actushumanus.com/'
SOURCE = 'Actus Humanus'
CITY = 'Gdańsk'
PROGRAM_URLS = (
    'https://www.actushumanus.com/pl/resurrectio.html',
    'https://www.actushumanus.com/pl/nativitas.html',
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def programme_year(soup):
    text = clean_text(soup)
    match = re.search(r'GDAŃSK\s*//\s*[^\n]{0,30}?\b(20\d{2})\b', text, re.I)
    return int(match.group(1)) if match else None


def parse_date(value, year):
    match = re.search(r'\b(\d{1,2})\s*[.-]\s*(\d{1,2})\b', value)
    if not match or year is None:
        return None
    try:
        return date(year, int(match.group(2)), int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\bgodz\.?\s*([01]?\d|2[0-3])[:.]([0-5]\d)\b', value, re.I)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def venue_name(value):
    # Programme locations append street addresses after a comma.  Addresses do
    # not belong in the venue field used by the ingestion pipeline.
    return re.split(r',\s*(?=(?:ul\.|al\.|plac\b|pl\.\b))', value, maxsplit=1, flags=re.I)[0].strip()


def parse_programme_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    year = programme_year(soup)
    records = []

    for card in soup.select('#program .tourKoncert'):
        title = clean_text(card.select_one('.kName'))
        date_box = clean_text(card.select_one('.data'))
        event_date = parse_date(date_box, year)
        venue = venue_name(clean_text(card.select_one('.miejsceKoncert')))
        if not title or not event_date or not venue:
            continue

        description_parts = [
            clean_text(card.select_one('.kName')),
            clean_text(card.select_one('.kDesc')),
            clean_text(card.select_one('.kDescAll')),
        ]
        description = '\n\n'.join(part for part in description_parts if part) or None
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(date_box),
            'venue': venue,
            'city': CITY,
            'country_code': 'PL',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    return records


class ActusHumanusComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='actushumanus_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for url in PROGRAM_URLS:
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Actus Humanus programme',
                    event='crawler_page_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            records.extend(parse_programme_page(response.text, url))

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    ActusHumanusComCrawler().run()


if __name__ == '__main__':
    main()
