import json
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://chamberorchestraofnewyork.org/'
SOURCE = 'Chamber Orchestra of New York'
EVENTS_API = urljoin(SOURCE_URL, 'wp-json/wp/v2/ajde_events')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_schema(soup):
    for script in soup.select('script[type="application/ld+json"], .evo_event_schema'):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data.get('@graph', []) if isinstance(data, dict) else data
        if isinstance(candidates, dict):
            candidates = [candidates]
        if isinstance(data, dict):
            candidates = [data, *candidates]
        for candidate in candidates if isinstance(candidates, list) else []:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                return candidate
    return None


def parse_display_date(value):
    match = re.search(
        r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
        r'(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+(\d{1,2}),\s+(20\d{2})\b',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return datetime.strptime(' '.join(match.groups()), '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_display_time(value):
    match = re.search(r'\b(\d{1,2}):([0-5]\d)\s*([ap])\.?m\.?', value, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{match.group(2)}'


def parse_location(description):
    normalized = re.sub(r'\s+', ' ', description)
    match = re.search(
        r'\bat\s+(Carnegie Hall(?:[’\']s)?\s+(?:Zankel|Weill|Stern)[^.,\n]*Hall|'
        r'(?:Zankel|Weill|Stern)[^.,\n]*Hall(?:\s+at\s+Carnegie Hall)?)',
        normalized,
        re.IGNORECASE,
    )
    if not match:
        return None
    venue = match.group(1).strip().rstrip('.')
    venue = re.sub(
        r"Carnegie Hall[’']s\s+(.+)", r'\1 at Carnegie Hall', venue, flags=re.IGNORECASE
    )
    return venue, 'New York'


def programme_url(event_url):
    path = urlparse(event_url).path
    if not path.startswith('/events/'):
        return None
    slug = path[len('/events/'):].strip('/')
    return urljoin(SOURCE_URL, f'{slug}/') if slug else None


def parse_event(session, item):
    event_url = item.get('link')
    if not event_url:
        return None

    response = session.get(event_url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    schema = event_schema(soup)
    if not schema:
        return None

    title = BeautifulSoup(schema.get('name', ''), 'html.parser').get_text(' ', strip=True)
    event_description = clean_text(soup.select_one('.eventon_desc_in'))
    if not event_description:
        event_description = BeautifulSoup(
            schema.get('description', ''), 'html.parser'
        ).get_text('\n', strip=True)
    location = parse_location(event_description)
    event_date = parse_display_date(event_description)
    time_from = parse_display_time(clean_text(soup.select_one('.evoet_c3')))
    if not title or not event_date or not location:
        return None

    description = event_description or None
    details_url = programme_url(event_url)
    if details_url:
        details_response = session.get(details_url, timeout=45)
        if details_response.ok:
            details_soup = BeautifulSoup(details_response.text, 'html.parser')
            details = clean_text(details_soup.select_one('article .entry-content'))
            if details:
                description = details

    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': event_url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class ChamberOrchestraOfNewYorkOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chamberorchestraofnewyork_org',
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
            response = session.get(
                EVENTS_API,
                params={'per_page': 100, 'orderby': 'date', 'order': 'desc'},
                timeout=45,
            )
            response.raise_for_status()
            items = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Chamber Orchestra of New York events',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_API,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for item in items:
            try:
                record = parse_event(session, item)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Chamber Orchestra of New York event detail',
                    event='crawler_event_fetch_failed',
                    level='warning',
                    url=item.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    ChamberOrchestraOfNewYorkOrgCrawler().run()


if __name__ == '__main__':
    main()
