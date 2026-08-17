import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.portlandchamberorchestra.org/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts')
SOURCE = 'Portland Chamber Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
    r'(?:January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+\d{1,2},\s+\d{4}\b'
)
TIME_RE = re.compile(r'\b(1[0-2]|0?[1-9]):([0-5]\d)\s*([AP]M)\b', re.IGNORECASE)
CITY_RE = re.compile(r'^([A-Za-z][A-Za-z .\'-]+),\s*OR\s+\d{5}(?:-\d{4})?$')


def clean_text(element):
    if element is None:
        return ''
    return re.sub(r'\s+', ' ', element.get_text(' ', strip=True)).strip()


def parse_datetime(value):
    date_match = DATE_RE.search(value)
    if not date_match:
        return None, None
    try:
        event_date = datetime.strptime(date_match.group(), '%A, %B %d, %Y').date().isoformat()
    except ValueError:
        return None, None

    time_match = TIME_RE.search(value)
    if not time_match:
        return event_date, None
    time_from = datetime.strptime(time_match.group(), '%I:%M %p').strftime('%H:%M')
    return event_date, time_from


def event_elements(title_heading):
    """Yield following tags until the next event heading."""
    for element in title_heading.next_elements:
        if element is title_heading or not isinstance(element, Tag):
            continue
        if element.name == 'h3':
            break
        yield element


def parse_event(title_heading):
    title = clean_text(title_heading)
    date_value = None
    location = None
    event_url = None

    for element in event_elements(title_heading):
        if element.name == 'h4' and date_value is None:
            date_value = clean_text(element)
        elif element.name == 'p' and location is None:
            if any(CITY_RE.fullmatch(text.strip()) for text in element.stripped_strings):
                location = element
        elif element.name == 'a' and element.get('href'):
            href = urljoin(CONCERTS_URL, element['href'])
            parsed = urlparse(href)
            if (
                'tickets & info' in clean_text(element).lower()
                and parsed.netloc == urlparse(SOURCE_URL).netloc
                and parsed.path not in {'/', '/concerts'}
            ):
                event_url = href

    event_date, time_from = parse_datetime(date_value or '')
    city_match = next(
        (CITY_RE.fullmatch(text.strip()) for text in location.stripped_strings if CITY_RE.fullmatch(text.strip())),
        None,
    )
    if not title or not event_date or not location or not city_match or not event_url:
        return None

    venue = next((re.sub(r'\s+', ' ', text).strip() for text in location.stripped_strings), '')
    city = city_match.group(1).strip()
    if not venue or not city:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': event_url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class PortlandChamberOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='portlandchamberorchestra_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
            response = requests.get(CONCERTS_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Portland Chamber Orchestra concerts',
                event='crawler_fetch_failed',
                level='error',
                url=CONCERTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        main_content = soup.select_one('main')
        if main_content is None:
            log_message(
                'Concert page has no main content',
                event='crawler_parse_failed',
                level='error',
                url=CONCERTS_URL,
            )
            return []

        records = []
        for heading in main_content.find_all('h3'):
            record = parse_event(heading)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    PortlandChamberOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
