import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.dcsmusic.org/'
SOURCE = 'Delaware County Symphony'
VENUE = 'Neumann University Meagher Theatre'
CITY = 'Aston'

SERIES_URLS = (
    'https://www.dcsmusic.org/symphony-series',
    'https://www.dcsmusic.org/chamber-series',
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'^(January|February|March|April|May|June|July|August|September|October|November|December) '
    r'\d{1,2}, 20\d{2}$'
)


def clean_lines(element):
    lines = []
    for value in element.get_text('\n', strip=True).splitlines():
        value = value.replace('\xa0', ' ').replace('\u200b', '').strip()
        value = re.sub(r'\s+', ' ', value)
        if value and value != '\u200b' and not re.fullmatch(r'_+', value):
            lines.append(value)
    return lines


def parse_event_block(lines, url):
    records = []
    date_indexes = [index for index, value in enumerate(lines) if DATE_RE.fullmatch(value)]

    for position, start in enumerate(date_indexes):
        end = date_indexes[position + 1] if position + 1 < len(date_indexes) else len(lines)
        details = lines[start + 1:end]
        if not details:
            continue

        try:
            event_date = datetime.strptime(lines[start], '%B %d, %Y').date().isoformat()
        except ValueError:
            continue

        title = details[0].strip()
        description = '\n'.join(details[1:]).strip() or None
        if not title:
            continue

        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': '15:00',
            'venue': VENUE,
            'city': CITY,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    return records


def parse_series_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for element in soup.select('[data-testid="richTextElement"]'):
        lines = clean_lines(element)
        if any(DATE_RE.fullmatch(value) for value in lines):
            records.extend(parse_event_block(lines, url))
    return records


class DcsmusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='dcsmusic_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []

        for url in SERIES_URLS:
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Delaware County Symphony series',
                    event='crawler_fetch_failed',
                    level='error',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise
            records.extend(parse_series_page(response.text, url))

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    DcsmusicOrgCrawler().run()


if __name__ == '__main__':
    main()
