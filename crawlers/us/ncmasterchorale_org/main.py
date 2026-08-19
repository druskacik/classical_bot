import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ncmasterchorale.org/'
SEASON_URL = urljoin(SOURCE_URL, 'season/')
SEASON_API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/pages?slug=season')
SOURCE = 'North Carolina Master Chorale'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_PATTERN = re.compile(
    r'^(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+'
    r'at\s+(?P<time>\d{1,2}(?::\d{2})?\s*[ap]m)\s*(?P<venue>.+)$',
    re.IGNORECASE,
)
SEASON_PATTERN = re.compile(r'(?P<start>\d{4})\s*[-–]\s*(?P<end>\d{4})\s+Season', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text).strip()


def parse_season_years(soup):
    heading = soup.find(['h1', 'h2'], string=SEASON_PATTERN)
    match = SEASON_PATTERN.search(clean_text(heading)) if heading else None
    if not match:
        return None
    return int(match.group('start')), int(match.group('end'))


def parse_event(container, season_years):
    title_node = container.find('h2')
    details_node = container.find('h4')
    title = clean_text(title_node)
    details = clean_text(details_node)
    match = DATE_PATTERN.match(details)
    if not title or not match:
        return None

    try:
        start_year, end_year = season_years
        month = datetime.strptime(match.group('month'), '%B').month
        year = start_year if month >= 7 else end_year
        event_date = datetime(year, month, int(match.group('day'))).date()
        event_time = datetime.strptime(
            re.sub(r'\s+', '', match.group('time')).upper(),
            '%I:%M%p' if ':' in match.group('time') else '%I%p',
        ).strftime('%H:%M')
    except ValueError:
        return None

    venue = clean_text(match.group('venue'))
    anchor = container.get('id') or title_node.get('id')
    if not venue or not anchor:
        return None

    description_parts = [
        clean_text(node) for node in container.select('.gs-toggler-wrapper p')
    ]
    description = '\n\n'.join(part for part in description_parts if part) or None
    return {
        'title': title,
        'date': event_date.isoformat(),
        'url': f'{SEASON_URL}#{anchor}',
        'time_from': event_time,
        'time_to': None,
        'venue': venue,
        'city': 'Raleigh',
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    response = session.get(SEASON_API_URL, headers=HEADERS, timeout=45)
    response.raise_for_status()
    pages = response.json()
    if not pages:
        log_message(
            'Season page is missing from WordPress API',
            event='crawler_empty_listing',
            level='warning',
            url=SEASON_API_URL,
            record_count=0,
        )
        return []

    soup = BeautifulSoup(pages[0].get('content', {}).get('rendered', ''), 'html.parser')
    season_years = parse_season_years(soup)
    if not season_years:
        log_message(
            'Could not determine years from season heading',
            event='crawler_parse_failed',
            level='warning',
            url=SEASON_URL,
        )
        return []

    records = []
    containers = soup.select('div.wp-block-columns[id]')
    for container in containers:
        record = parse_event(container, season_years)
        if record:
            records.append(record)
        else:
            log_message(
                'Skipping season entry with incomplete required fields',
                event='crawler_event_skipped',
                level='warning',
                url=SEASON_URL,
            )

    if not records:
        log_message(
            'No concerts found on season page',
            event='crawler_empty_listing',
            level='warning',
            url=SEASON_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class NcMasterChoraleOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ncmasterchorale_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'time_to', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    NcMasterChoraleOrgCrawler().run()


if __name__ == '__main__':
    main()
