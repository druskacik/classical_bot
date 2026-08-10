import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://efo.ee/'
PROGRAM_URL = 'https://efo.ee/coming-up/'
API_URL = (
    'https://efo.ee/wp-json/wp/v2/pages'
    '?slug=coming-up&_fields=link,modified,content,title'
)
SOURCE = 'Estonian Festival Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-GB,en;q=0.9,et;q=0.7',
}

MONTHS = {
    'january': 1,
    'february': 2,
    'march': 3,
    'april': 4,
    'may': 5,
    'june': 6,
    'july': 7,
    'august': 8,
    'september': 9,
    'october': 10,
    'november': 11,
    'december': 12,
}

# The page prints the hall but not a separate city. These venue names uniquely
# identify their locations. Unknown touring venues are skipped rather than
# being assigned the orchestra's Estonian home location.
VENUES = {
    'Pärnu Concert Hall': ('Pärnu', 'EE'),
    'Musik- und Kongresshalle Lübeck': ('Lübeck', 'DE'),
}

HEADING_RE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
    r'(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})\s+at\s+'
    r'(?P<time>[01]?\d|2[0-3]):(?P<minute>[0-5]\d),\s+(?P<venue>.+)$'
)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event_heading(value, year):
    match = HEADING_RE.match(clean_text(value))
    if not match:
        return None
    month = MONTHS.get(match.group('month').lower())
    venue = clean_text(match.group('venue'))
    location = VENUES.get(venue)
    if not month or not location:
        return None
    try:
        event_date = date(year, month, int(match.group('day'))).isoformat()
    except ValueError:
        return None
    city, country_code = location
    return {
        'date': event_date,
        'time_from': f"{int(match.group('time')):02d}:{match.group('minute')}",
        'venue': venue,
        'city': city,
        'country_code': country_code,
    }


def parse_page(payload):
    modified = payload.get('modified', '')
    year_match = re.match(r'(?P<year>20\d{2})-', modified)
    if not year_match:
        return []
    year = int(year_match.group('year'))
    html = payload.get('content', {}).get('rendered', '')
    soup = BeautifulSoup(html, 'html.parser')
    paragraphs = soup.select('.fusion-text p')
    records = []

    for index, paragraph in enumerate(paragraphs):
        strong = paragraph.find('strong')
        if not strong:
            continue
        location = parse_event_heading(strong.get_text(' ', strip=True), year)
        if not location:
            heading = clean_text(strong.get_text(' ', strip=True))
            if HEADING_RE.match(heading):
                log_message(
                    'Skipping EFO concert with an unknown venue',
                    event='crawler_item_skipped',
                    level='warning',
                    url=PROGRAM_URL,
                    venue=HEADING_RE.match(heading).group('venue'),
                )
            continue

        description_parts = []
        for detail in paragraphs[index + 1:]:
            detail_strong = detail.find('strong')
            if detail_strong and HEADING_RE.match(
                clean_text(detail_strong.get_text(' ', strip=True))
            ):
                break
            text = clean_text(detail.get_text('\n', strip=True))
            if text and text.lower() != 'pärnu music festival programme':
                description_parts.append(text)

        venue = location['venue']
        records.append({
            'title': f'{SOURCE} at {venue}',
            **location,
            'url': PROGRAM_URL,
            'description': clean_text('\n\n'.join(description_parts)) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    return records


def get_concerts():
    try:
        response = requests.get(API_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        pages = response.json()
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Failed to fetch EFO concert programme',
            event='crawler_fetch_failed',
            level='error',
            url=API_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    if not pages:
        return []
    return sorted(
        parse_page(pages[0]),
        key=lambda record: (record['date'], record['time_from'], record['venue']),
    )


class EfoEeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='efo_ee',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='EE',
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
        return get_concerts()


def main():
    EfoEeCrawler().run()


if __name__ == '__main__':
    main()
