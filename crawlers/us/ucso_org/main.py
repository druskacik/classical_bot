import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ucso.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
SOURCE = 'University City Symphony Orchestra'
TITLE = 'University City Symphony Orchestra Concert'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s+'
    r'(\d{1,2}(?::\d{2})?\s*[AP]M)\b',
    re.IGNORECASE,
)

VENUES = ('Kirkwood Performing Arts Center', '560 Music Center')
CITIES = ('University City', 'St. Louis', 'Kirkwood')


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        return datetime.strptime(value, '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    normalized = re.sub(r'\s+', ' ', value.upper()).strip()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_program_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    if not main:
        return None

    description = clean_text(main.get_text('\n', strip=True))
    date_time = DATE_TIME_RE.search(description)
    if not date_time:
        return None

    event_date = parse_date(date_time.group(1))
    time_from = parse_time(date_time.group(2))
    venue = next((name for name in VENUES if name.lower() in description.lower()), None)
    city = next(
        (
            name
            for name in CITIES
            if re.search(rf'\b{re.escape(name)},\s*MO\b', description, re.IGNORECASE)
        ),
        None,
    )
    if not event_date or not time_from or not venue or not city:
        return None

    return {
        'title': TITLE,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    response = session.get(CALENDAR_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    urls = []
    for link in soup.select('a[href]'):
        if clean_text(link.get_text(' ', strip=True)).lower() != 'concert program':
            continue
        url = urljoin(CALENDAR_URL, link.get('href'))
        if url.startswith(SOURCE_URL) and url not in urls:
            urls.append(url)

    records = []
    for url in urls:
        try:
            detail_response = session.get(url, timeout=45)
            detail_response.raise_for_status()
            record = parse_program_page(detail_response.text, url)
        except requests.RequestException as error:
            log_message(
                'Concert programme request failed',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue

        if record:
            records.append(record)
        else:
            log_message(
                'Concert programme could not be parsed',
                event='crawler_detail_unparseable',
                level='warning',
                url=url,
            )

    if not records:
        log_message(
            'No concert programme records found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['url']))


class UcsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ucso_org',
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
        dedupe_subset=['date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    UcsoOrgCrawler().run()


if __name__ == '__main__':
    main()
