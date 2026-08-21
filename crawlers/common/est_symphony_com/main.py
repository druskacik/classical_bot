import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'http://est-symphony.com/'
SOURCE = 'E.S.T. Symphony'
EVENT_PAGES = (
    f'{SOURCE_URL}dates.html',
    f'{SOURCE_URL}past-events.html',
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

COUNTRY_BY_CITY = {
    'Basel': 'CH',
    'Göteborg': 'SE',
    'Hamburg': 'DE',
    'Istanbul': 'TR',
    'Jena': 'DE',
    'Ludwigshafen': 'DE',
    'Rotterdam': 'NL',
    'St. Pölten': 'AT',
    'Stavanger': 'NO',
    'Stockholm': 'SE',
    'Stuttgart': 'DE',
    'Wien': 'AT',
}

HEADING_PATTERN = re.compile(
    r'^(?P<dates>\d{1,2}\.\d{1,2}\.\d{4}|'
    r'\d{1,2}\.\s*\+\s*\d{1,2}\.\d{1,2}\.\d{4})'
    r'\s+(?P<city>.+?)\s+[\-–—]\s+(?P<venue>.+)$'
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_dates(value):
    full_date = re.fullmatch(r'(\d{1,2})\.(\d{1,2})\.(\d{4})', value)
    if full_date:
        day, month, year = map(int, full_date.groups())
        try:
            return [date(year, month, day).isoformat()]
        except ValueError:
            return []

    combined = re.fullmatch(
        r'(\d{1,2})\.\s*\+\s*(\d{1,2})\.(\d{1,2})\.(\d{4})', value
    )
    if not combined:
        return []
    first_day, second_day, month, year = map(int, combined.groups())
    try:
        return [
            date(year, month, first_day).isoformat(),
            date(year, month, second_day).isoformat(),
        ]
    except ValueError:
        return []


def parse_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for heading in soup.select('main h2, #main h2'):
        heading_text = clean_text(heading)
        match = HEADING_PATTERN.fullmatch(heading_text)
        if not match:
            continue

        city = clean_text(match.group('city'))
        venue = clean_text(match.group('venue'))
        country_code = COUNTRY_BY_CITY.get(city)
        event_dates = parse_dates(match.group('dates'))
        if not city or not venue or not country_code or not event_dates:
            log_message(
                'Skipped incomplete E.S.T. Symphony event',
                event='crawler_item_skipped',
                level='warning',
                url=url,
                error_type='IncompleteEventData',
                error_message='Required date, venue, city, or country could not be resolved',
            )
            continue

        container = heading.find_parent('div', class_='ce_text')
        description = clean_text(container) if container else ''
        description = description.removeprefix(heading_text).strip() or None
        for event_date in event_dates:
            records.append({
                'title': SOURCE,
                'date': event_date,
                'url': url,
                'time_from': None,
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class EstSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='est_symphony_com',
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
        records = []
        session = requests.Session()
        session.headers.update(HEADERS)
        for url in EVENT_PAGES:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            records.extend(parse_page(response.text, url))
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['city'], item['venue']
            ),
        )


def main():
    EstSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
