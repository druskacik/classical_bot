import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://norwichphil.org.uk/'
SOURCE = 'Norwich Philharmonic Society'
PAGES_API = f'{SOURCE_URL}wp-json/wp/v2/pages'
CONCERTS_PAGE_ID = 542
CITY = 'Norwich'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

DETAIL_SLUG_RE = re.compile(r'^concert-details-\d+$')
DATE_RE = re.compile(
    r'\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?\s+'
    r'(?P<day>\d{1,2})\s+'
    r'(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'(?P<year>20\d{2})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalise_time(match):
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour < 1 or hour > 12 or minute > 59:
        return None
    if match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def normalise_venue(value):
    key = re.sub(r'[^a-z]', '', value.lower())
    if key in {'standrewshall', 'saintandrewshall'}:
        return 'St Andrew’s Hall'
    if key == 'thekingscentre':
        return 'The King’s Centre'
    return value


def parse_event(page):
    content = page.get('content', {}).get('rendered', '')
    soup = BeautifulSoup(content, 'html.parser')
    details = soup.select_one('.elementor-widget-text-editor .elementor-widget-container')
    if not details:
        return []

    title = clean_text(details.select_one('h4'))
    if title == 'NorthernLights':
        title = 'Northern Lights'
    paragraphs = details.select('p')
    if not title or not paragraphs:
        return []

    occurrence_text = clean_text(paragraphs[0])
    date_match = DATE_RE.search(occurrence_text)
    if not date_match:
        return []
    try:
        event_date = datetime.strptime(
            f"{date_match.group('day')} {date_match.group('month')} {date_match.group('year')}",
            '%d %B %Y',
        ).date().isoformat()
    except ValueError:
        return []

    lines = [line.strip(' –-') for line in occurrence_text.splitlines() if line.strip()]
    venue = next(
        (
            line
            for line in lines
            if not DATE_RE.search(line)
            and line.lower() != 'venue details'
            and not TIME_RE.search(line)
        ),
        '',
    )
    venue = re.sub(r'\s*[–-]\s*Venue details\s*$', '', venue, flags=re.IGNORECASE).strip()
    venue = re.sub(r',\s*Norwich\s*$', '', venue, flags=re.IGNORECASE).strip()
    venue = normalise_venue(venue)
    if not venue:
        return []

    times = []
    for match in TIME_RE.finditer(occurrence_text):
        value = normalise_time(match)
        if value and value not in times:
            times.append(value)
    if not times:
        times = [None]

    description_parts = []
    for paragraph in paragraphs[1:]:
        text = clean_text(paragraph)
        if not text or re.search(r'\bTickets?\s+(?:priced|£|are available)', text, re.IGNORECASE):
            continue
        description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    return [
        {
            'title': title,
            'date': event_date,
            'url': page['link'],
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'GB',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for time_from in times
    ]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    response = session.get(
        PAGES_API,
        params={
            'parent': CONCERTS_PAGE_ID,
            'per_page': 100,
            '_fields': 'slug,link,content',
        },
        timeout=45,
    )
    response.raise_for_status()

    records = []
    for page in response.json():
        if not DETAIL_SLUG_RE.fullmatch(page.get('slug', '')):
            continue
        try:
            records.extend(parse_event(page))
        except (KeyError, TypeError, ValueError) as error:
            log_message(
                'Failed to parse Norwich Philharmonic concert page',
                event='crawler_item_failed',
                level='warning',
                url=page.get('link'),
                error_type=type(error).__name__,
                error_message=str(error),
            )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class NorwichPhilOrgUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='norwichphil_org_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
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
    NorwichPhilOrgUkCrawler().run()


if __name__ == '__main__':
    main()
