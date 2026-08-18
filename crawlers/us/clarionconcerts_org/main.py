import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.clarionconcerts.org/'
COLLECTION_URL = urljoin(SOURCE_URL, 'our-concert')
SOURCE = 'Clarion Concerts'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(January|February|March|April|May|June|July|August|September|October|'
    r'November|December)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(?:at\s+)?(\d{1,2})(?::([0-5]\d))?\s*([AP])M\b', re.IGNORECASE)
CITY_RE = re.compile(r',\s*([^,\n]+),\s*(?:NY|New York)\s+\d{5}\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.translate(str.maketrans({
        '\u0420': 'P', '\u041e': 'O', '\u0421': 'C', '\u041a': 'K', '\u0415': 'E',
        '\u0422': 'T', '\u0412': 'B', '\u041c': 'M', '\u2028': '\n',
    }))
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\u200d', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(value)
    if not match:
        return None
    try:
        return datetime.strptime(' '.join(match.groups()), '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not 1 <= hour <= 12:
        return None
    if match.group(3).upper() == 'P' and hour != 12:
        hour += 12
    elif match.group(3).upper() == 'A' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def parse_location(lines):
    for index, line in enumerate(lines):
        city_match = CITY_RE.search(line)
        if not city_match:
            continue

        city = city_match.group(1).strip()
        venue = ''
        address_start = re.search(r',\s*\d+\s+', line)
        if address_start:
            venue = line[:address_start.start()].strip(' ,')
            if not venue:
                venue_parts = []
                for previous in reversed(lines[max(0, index - 2):index]):
                    if DATE_RE.search(previous):
                        break
                    venue_parts.insert(0, previous.strip(' ,'))
                venue = ' '.join(venue_parts)
        elif index > 0:
            venue = lines[index - 1].strip(' ,')

        if city and venue and not DATE_RE.search(venue):
            return venue, city.title()
    return None


def parse_item(item):
    title = clean_text(item.get('title'))
    description = clean_text(item.get('excerpt') or item.get('body'))
    lines = [line for line in description.splitlines() if line]
    event_date = parse_date(description)
    location = parse_location(lines)
    path = item.get('fullUrl')
    if not title or not event_date or not location or not path:
        return None

    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, path),
        'time_from': parse_time(description),
        'venue': venue,
        'city': city,
        'description': description or None,
    }


class ClarionConcertsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='clarionconcerts_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        try:
            response = requests.get(
                COLLECTION_URL,
                params={'format': 'json', 'offset': 0},
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Clarion Concerts collection',
                event='crawler_fetch_failed',
                level='error',
                url=COLLECTION_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for item in payload.get('items', []):
            record = parse_item(item)
            if record:
                records.append(record)

        if not records:
            log_message(
                'No parseable Clarion Concerts events found',
                event='crawler_empty_listing',
                level='warning',
                url=COLLECTION_URL,
                record_count=0,
            )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    ClarionConcertsOrgCrawler().run()


if __name__ == '__main__':
    main()
