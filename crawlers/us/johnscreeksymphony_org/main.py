import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.johnscreeksymphony.org/'
SOURCE = 'Johns Creek Symphony Orchestra'
SEASON_URL = urljoin(SOURCE_URL, 'season20')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_PATTERN = re.compile(
    r'^(January|February|March|April|May|June|July|August|September|October|November|December) '
    r'\d{1,2}, 20\d{2}$'
)
CITY_PATTERN = re.compile(r'^(.+),\s*GA$')


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def detail_links(soup):
    links = []
    for anchor in soup.select('main a[href]'):
        if clean_text(anchor.get_text(' ', strip=True)).lower() != 'details':
            continue
        url = urljoin(SEASON_URL, anchor['href'])
        if url.startswith(SOURCE_URL) and url not in links:
            links.append(url)
    return links


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    title_element = main.select_one('h1') if main else None
    title = clean_text(title_element.get_text(' ', strip=True) if title_element else '')
    strings = [clean_text(value) for value in main.stripped_strings] if main else []

    date_index = next((i for i, value in enumerate(strings) if DATE_PATTERN.fullmatch(value)), None)
    if date_index is None or date_index + 2 >= len(strings):
        return None

    date_text = strings[date_index]
    venue = strings[date_index + 1]
    city_match = CITY_PATTERN.fullmatch(strings[date_index + 2])
    if not title or not city_match or not venue or 'tba' in venue.lower():
        return None

    try:
        event_date = datetime.strptime(date_text, '%B %d, %Y').date().isoformat()
    except ValueError:
        return None

    # Detail pages repeat date and location under a "Details" heading. Keeping
    # the remaining text preserves programme and artist notes for later work
    # extraction while avoiding ticket-sale form boilerplate near the top.
    details_index = next(
        (i for i, value in enumerate(strings) if value.rstrip(':').lower() == 'details'),
        None,
    )
    description_parts = strings[date_index + 3:]
    if details_index is not None:
        description_parts = strings[date_index + 3:details_index]
        program_index = next(
            (i for i in range(details_index + 1, len(strings))
             if strings[i].rstrip(':').lower() == 'program'),
            None,
        )
        if program_index is not None:
            description_parts += strings[program_index:]

    description = '\n'.join(dict.fromkeys(description_parts)).strip() or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': None,
        'venue': venue,
        'city': city_match.group(1).strip(),
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class JohnsCreekSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='johnscreeksymphony_org',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(SEASON_URL, timeout=45)
            response.raise_for_status()
            links = detail_links(BeautifulSoup(response.text, 'html.parser'))
            records = []
            for url in links:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
                record = parse_detail(detail_response.text, url)
                if record:
                    records.append(record)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Johns Creek Symphony schedule',
                event='crawler_fetch_failed',
                level='error',
                url=SEASON_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        if not records:
            log_message(
                'No Johns Creek Symphony concerts found',
                event='crawler_empty_listing',
                level='warning',
                url=SEASON_URL,
                record_count=0,
            )
        return sorted(records, key=lambda record: (record['date'], record['title']))


def main():
    JohnsCreekSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
