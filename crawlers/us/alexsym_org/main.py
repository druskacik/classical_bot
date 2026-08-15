import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://alexsym.org/'
SOURCE = 'Alexandria Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/performance'
CITY = 'Alexandria'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s+at\s+'
    r'(\d{1,2}:\d{2})\s*([ap]m)',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    text = soup.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_occurrence(item):
    text = clean_text(item)
    match = DATE_TIME_RE.search(text)
    venue_elements = [
        element for element in item.find_all('i')
        if 'fa' not in (element.get('class') or []) and clean_text(element)
    ]
    venue = clean_text(venue_elements[-1]) if venue_elements else ''
    if not match or not venue:
        return None

    try:
        event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
        time_from = datetime.strptime(
            f'{match.group(2)} {match.group(3).upper()}', '%I:%M %p'
        ).strftime('%H:%M')
    except ValueError:
        return None
    return event_date, time_from, venue


def api_performances(session):
    page = 1
    performances = []
    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page, '_fields': 'id,link,title,content'},
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError('Unexpected WordPress performance API response')
        performances.extend(payload)

        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return performances
        page += 1


def performance_records(session, performance):
    url = performance.get('link') or ''
    title = clean_text((performance.get('title') or {}).get('rendered'))
    if not url or not title:
        return []

    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    description = clean_text((performance.get('content') or {}).get('rendered')) or None

    records = []
    for item in soup.select('ul.performances > li'):
        occurrence = parse_occurrence(item)
        if occurrence is None:
            continue
        event_date, time_from, venue = occurrence
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class AlexsymOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='alexsym_org',
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
        try:
            performances = api_performances(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Alexandria Symphony performance API',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for performance in performances:
            try:
                records.extend(performance_records(session, performance))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Alexandria Symphony performance detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=performance.get('link') or SOURCE_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    AlexsymOrgCrawler().run()


if __name__ == '__main__':
    main()
