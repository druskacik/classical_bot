import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.filarmonicapisana.it/'
EVENTS_URL = urljoin(SOURCE_URL, 'it/eventi')
SOURCE = 'Società Filarmonica Pisana'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

CITY_PATTERNS = (
    ('San Giuliano Terme', r'\bSan Giuliano Terme\b'),
    ('Marina di Pisa', r'\bMarina di Pisa\b'),
    ('Sant\'Anna di Stazzema', r"\bSant['’]Anna di Stazzema\b"),
    ('Pontedera', r'\bPontedera\b'),
    ('Ghezzano', r'\bGhezzano\b'),
    ('Calci', r'\bCalci\b'),
    ('Pisa', r'\bPisa\b'),
)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def parse_location(value):
    location = clean_text(value)
    city = next((name for name, pattern in CITY_PATTERNS if re.search(pattern, location, re.I)), None)
    if not city:
        # The Centro SMS is a well-established Pisa venue; some archive rows omit the city.
        if re.search(r'Centro (?:Espositivo )?SMS', location, re.I):
            city = 'Pisa'
        else:
            return None

    venue = location
    venue = re.sub(r'\s*[-,]?\s*(?:via|viale|piazza)\b.*$', '', venue, flags=re.I)
    if city != 'Pisa':
        venue = re.sub(r'\s*-\s*Pisa\s*$', '', venue, flags=re.I)
    venue = re.sub(r'\s*[-,(]?\s*' + re.escape(city) + r'(?:\s*\(PI\))?\)?\s*$', '', venue, flags=re.I)
    venue = re.sub(r'\s*-\s*$', '', venue).strip(' ,-')
    venue = re.sub(r'\s+di$', '', venue, flags=re.I).strip()

    # Address-only locations do not provide a defensible venue name.
    if not venue or re.match(r'^(?:via|viale|piazza)\b', venue, re.I):
        return None
    return venue, city


def parse_row(row, page_url):
    title_link = row.select_one('.views-field-title a[href]')
    date_node = row.select_one('[property="dc:date"][content]')
    location_node = row.select_one('.views-field-field-luogo .field-content')
    if not title_link or not date_node or not location_node:
        return None

    title = clean_text(title_link)
    location = parse_location(location_node)
    raw_date = date_node.get('content', '')
    try:
        occurrence = datetime.fromisoformat(raw_date)
    except ValueError:
        return None
    if not title or not location:
        return None

    venue, city = location
    return {
        'title': title,
        'date': occurrence.date().isoformat(),
        'url': urljoin(page_url, title_link['href']),
        'time_from': occurrence.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class FilarmonicaPisanaItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filarmonicapisana_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page_number = 0

        while True:
            params = {'page': page_number} if page_number else None
            page_url = EVENTS_URL if not params else f'{EVENTS_URL}?page={page_number}'
            try:
                soup = get_soup(session, EVENTS_URL, params=params)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Filarmonica Pisana events page',
                    event='crawler_fetch_failed',
                    level='error',
                    url=page_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            rows = soup.select('.view-content table.views-view-grid > tbody > tr')
            if not rows:
                break

            for row in rows:
                record = parse_row(row, page_url)
                if not record:
                    continue
                try:
                    detail = get_soup(session, record['url'])
                    body = detail.select_one('.node-event .field-name-body')
                    subtitle = detail.select_one('.node-event .field-name-field-sottotitolo')
                    parts = [clean_text(subtitle), clean_text(body)]
                    record['description'] = '\n\n'.join(part for part in parts if part) or None
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Filarmonica Pisana event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=record['url'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                records.append(record)

            next_link = soup.select_one('li.pager-next a[href]')
            if not next_link:
                break
            page_number += 1

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    FilarmonicaPisanaItCrawler().run()


if __name__ == '__main__':
    main()
