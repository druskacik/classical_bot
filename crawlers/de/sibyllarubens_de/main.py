import re
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sibyllarubens.de/'
EVENTS_URL = urljoin(SOURCE_URL, 'veranstaltungen')
SOURCE = 'Sibylla Rubens'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

# The calendar has no structured location fields. These rules cover the venues
# used in its current and archived entries, including touring engagements.
LOCATION_RULES = [
    (r'schloss vietgest', 'Schloss Vietgest', 'Lalendorf', 'DE'),
    (r'kloster roggenburg', 'Kloster Roggenburg', 'Roggenburg', 'DE'),
    (r'stadthalle reutlingen', 'Stadthalle Reutlingen', 'Reutlingen', 'DE'),
    (r'stiftskirche t[üu]bingen', 'Stiftskirche Tübingen', 'Tübingen', 'DE'),
    (r'kirche pfrondorf', 'Kirche Pfrondorf', 'Tübingen', 'DE'),
    (r'm[üu]nster [üu]berlingen', 'Münster Überlingen', 'Überlingen', 'DE'),
    (
        r'landesmusikakademie sondershausen',
        'Landesmusikakademie Sondershausen',
        'Sondershausen',
        'DE',
    ),
    (r'schloss filseck', 'Schloss Filseck', 'Uhingen', 'DE'),
    (r'teatro del bicentenario', 'Teatro del Bicentenario', 'San Juan', 'AR'),
    (
        r'iglesia metodista(?: de)? bariloche|camba.*bariloche',
        'Iglesia Metodista de Bariloche',
        'San Carlos de Bariloche',
        'AR',
    ),
    (
        r'iglesia metodista san juan',
        'Iglesia Metodista San Juan',
        'San Juan',
        'AR',
    ),
    (r'trogen.*evang', 'Evangelische Kirche Trogen', 'Trogen', 'CH'),
    (r'm[üu]nsterkonzert freiburg', 'Freiburger Münster', 'Freiburg im Breisgau', 'DE'),
]


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\ufeff', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_dates(value):
    """Return explicit dates; for a continuous range, use its advertised start."""
    dates = []
    date_range = re.search(
        r'(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(?:bis|-)\s*'
        r'\d{1,2}\s*\.\s*\d{1,2}\s*\.\s*(20\d{2})',
        value,
        re.I,
    )
    if date_range:
        try:
            return [
                date(
                    int(date_range.group(3)),
                    int(date_range.group(2)),
                    int(date_range.group(1)),
                ).isoformat()
            ]
        except ValueError:
            return []

    slash = re.search(
        r'(\d{1,2})\s*\.\s*/\s*(\d{1,2})\s*\.'
        r'(?:\s*/\s*(\d{1,2})\s*\.)?\s*(\d{1,2})\s*\.\s*(20\d{2})',
        value,
    )
    if slash:
        days = [slash.group(1), slash.group(2), slash.group(3)]
        month, year = int(slash.group(4)), int(slash.group(5))
        for day_value in days:
            if day_value:
                try:
                    dates.append(date(year, month, int(day_value)).isoformat())
                except ValueError:
                    pass
        return dates

    for day_value, month_value, year_value in re.findall(
        r'(?<![\d.])(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(20\d{2})', value
    ):
        try:
            parsed = date(int(year_value), int(month_value), int(day_value)).isoformat()
        except ValueError:
            continue
        if parsed not in dates:
            dates.append(parsed)
    return dates


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])[.:]([0-5]\d)\s*Uhr\b', value, re.I)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_location(value):
    normalized = value.lower()
    for pattern, venue, city, country_code in LOCATION_RULES:
        if re.search(pattern, normalized, re.S):
            return venue, city, country_code
    return None


def external_url(detail):
    for link in detail.select('a[href]'):
        url = urljoin(EVENTS_URL, link.get('href', ''))
        if urlparse(url).scheme in {'http', 'https'}:
            return url
    return EVENTS_URL


def parse_row(row):
    wrapper = row.find('div', class_='dmRespColsWrapper', recursive=False)
    if wrapper is None:
        return []
    columns = wrapper.find_all('div', class_='dmRespCol', recursive=False)
    if len(columns) < 2:
        return []

    date_text = clean_text(columns[0])
    dates = parse_dates(date_text)
    detail = columns[1]
    detail_text = clean_text(detail)
    location = parse_location(detail_text)
    if not dates or not detail_text or location is None:
        return []

    heading = detail.find(re.compile(r'^h[1-6]$'))
    title = re.sub(r'\s+', ' ', clean_text(heading)).strip()
    if not title:
        title = next((line for line in detail_text.splitlines() if line.strip()), '')
    if not title:
        return []

    venue, city, country_code = location
    url = external_url(detail)
    description = detail_text or None
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(date_text),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
    ]


class SibyllarubensDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sibyllarubens_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(EVENTS_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Sibylla Rubens events',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for row in soup.select('#dm_content .dmRespRow'):
            records.extend(parse_row(row))
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    SibyllarubensDeCrawler().run()


if __name__ == '__main__':
    main()
