import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mojcaerdmann.com/'
SOURCE = 'Mojca Erdmann'
PAGES = (
    urljoin(SOURCE_URL, 'schedule/'),
    urljoin(SOURCE_URL, 'past-performances/'),
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9,de;q=0.7',
}

MONTHS = {
    'january': 1, 'february': 2, 'februar': 2, 'march': 3, 'märz': 3,
    'april': 4, 'may': 5, 'mai': 5, 'june': 6, 'july': 7,
    'august': 8, 'september': 9, 'october': 10, 'november': 11,
    'december': 12, 'dezember': 12,
}
MONTH_PATTERN = '|'.join(sorted(MONTHS, key=len, reverse=True))

COUNTRY_CODES = {
    'austra': 'AT', 'austria': 'AT', 'belgium': 'BE', 'bulgaria': 'BG',
    'czech republic': 'CZ', 'denmark': 'DK', 'finland': 'FI', 'france': 'FR',
    'gemany': 'DE', 'germany': 'DE', 'deutschland': 'DE', 'hungary': 'HU',
    'israel': 'IL', 'italy': 'IT', 'italien': 'IT', 'japan': 'JP',
    'luxembourg': 'LU', 'netherlands': 'NL', 'norway': 'NO', 'poland': 'PL',
    'romania': 'RO', 'slovenia': 'SI', 'spain': 'ES', 'sweden': 'SE',
    'switzerland': 'CH', 'turkey': 'TR', 'usa': 'US',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_dates(value):
    """Expand the site's lists such as 'April 3, 6 & 9, 2013'."""
    value = re.sub(r'^\(Deutsch\)\s*', '', value, flags=re.I).strip()
    value = re.sub(
        rf'\b(\d{{1,2}})\.\s*({MONTH_PATTERN})\b',
        lambda match: f'{match.group(2)} {match.group(1)}',
        value,
        flags=re.I,
    )
    if re.search(
        rf'\d\s*[-–]\s*(?:(?:{MONTH_PATTERN})\s+)?\d', value, flags=re.I
    ):
        return []
    years = [int(item) for item in re.findall(r'\b(20\d{2})\b', value)]
    fallback_year = years[-1] if years else None
    matches = list(re.finditer(rf'\b({MONTH_PATTERN})\b', value, flags=re.I))
    parsed = []

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        segment = value[match.end():end]
        segment_years = [int(item) for item in re.findall(r'\b(20\d{2})\b', segment)]
        event_year = segment_years[-1] if segment_years else fallback_year
        if event_year is None or re.search(r'\d\s*-\s*\d', segment):
            continue

        month = MONTHS[match.group(1).casefold()]
        days = [
            int(item) for item in re.findall(r'(?<!\d)(\d{1,2})(?!\d)', segment)
            if int(item) != event_year
        ]
        for day in days:
            try:
                parsed.append(date(event_year, month, day).isoformat())
            except ValueError:
                continue

    return list(dict.fromkeys(parsed))


def parse_location(value):
    parts = [part.strip() for part in value.split(',') if part.strip()]
    if len(parts) != 2:
        return None
    city, country = parts
    if '&' in city or '/' in city:
        return None
    country = re.sub(r'^\(Deutsch\)\s*', '', country, flags=re.I)
    country = re.sub(r'^.*?\[en\]', '', country, flags=re.I).strip()
    country_code = COUNTRY_CODES.get(country.casefold())
    if not city or city.casefold() == country.casefold() or not country_code:
        return None
    return city, country_code


def parse_event(element, page_url):
    title = clean_text(element.select_one('.event-title'))
    venue = clean_text(element.select_one('.gig-venue'))
    location = parse_location(clean_text(element.select_one('.gig-location')))
    event_dates = parse_dates(clean_text(element.select_one('.gig-dates-list')))
    if not title or not venue or not location or not event_dates:
        return []

    link = element.select_one('a[href]')
    event_url = urljoin(page_url, link['href']) if link else page_url
    description = clean_text(element.select_one('.gig-notes')) or None
    city, country_code = location
    return [
        {
            'title': title,
            'date': event_date,
            'url': event_url,
            'time_from': None,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in event_dates
    ]


class MojcaErdmannComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mojcaerdmann_com',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for page_url in PAGES:
            try:
                response = session.get(page_url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Mojca Erdmann performances',
                    event='crawler_fetch_failed',
                    level='error',
                    url=page_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            # The source contains invalid historic nesting that the lxml parser
            # repairs in the same way as browsers; html.parser truncates items.
            soup = BeautifulSoup(response.text, 'lxml')
            for element in soup.select('.event-wrap'):
                records.extend(parse_event(element, page_url))

        return sorted(
            records,
            key=lambda item: (item['date'], item['title'], item['venue'], item['city']),
        )


def main():
    MojcaErdmannComCrawler().run()


if __name__ == '__main__':
    main()
