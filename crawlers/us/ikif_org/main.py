import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ikif.org/'
SCHEDULE_URL = f'{SOURCE_URL}Schedule'
SOURCE = 'International Keyboard Institute & Festival'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUES = {
    'klavierhaus': 'Klavierhaus',
    'merkin hall': 'Merkin Hall at Kaufman Music Center',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_year(soup):
    title = clean_text(soup.title)
    match = re.search(r'\b(20\d{2})\b', title)
    if not match:
        raise ValueError('Could not determine the schedule year from the page title')
    return int(match.group(1))


def parse_date(value, year):
    try:
        parsed = datetime.strptime(value.strip(), '%A, %B %d')
        return date(year, parsed.month, parsed.day).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.match(r'\s*(\d{1,2}:\d{2}\s*[ap]m)\s*:', value, re.IGNORECASE)
    if not match:
        return None
    try:
        return datetime.strptime(
            re.sub(r'\s+', '', match.group(1)).upper(), '%I:%M%p'
        ).strftime('%H:%M')
    except ValueError:
        return None


def parse_venue(title):
    lowered = title.lower()
    for marker, venue in VENUES.items():
        if marker in lowered:
            return venue
    return None


def parse_event(item, event_date):
    title_element = item.find('b')
    title = re.sub(r'\s+', ' ', clean_text(title_element)).strip()
    time_from = parse_time(clean_text(item))
    venue = parse_venue(title)
    if not title or not event_date or not time_from or not venue:
        return None

    description_parts = []
    for sibling in title_element.next_siblings:
        if getattr(sibling, 'name', None) == 'br':
            continue
        text = sibling.get_text(' ', strip=True) if hasattr(sibling, 'get_text') else str(sibling)
        text = re.sub(r'\s+', ' ', text).strip()
        if text:
            description_parts.append(text)

    return {
        'title': title,
        'date': event_date,
        'url': SCHEDULE_URL,
        'time_from': time_from,
        'venue': venue,
        'city': 'New York',
        'country_code': 'US',
        'description': '\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class IkifOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ikif_org',
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
            response = requests.get(SCHEDULE_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch IKIF schedule',
                event='crawler_fetch_failed',
                level='error',
                url=SCHEDULE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        year = parse_year(soup)
        records = []
        for heading in soup.select('main p.fs-4'):
            event_date = parse_date(clean_text(heading), year)
            if not event_date:
                continue
            section = heading.find_parent('div', class_='container-fluid')
            event_row = section.find_next_sibling('div', class_='row') if section else None
            if event_row is None:
                continue
            for item in event_row.select('li'):
                record = parse_event(item, event_date)
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'], record['title'], record['venue']
            ),
        )


def main():
    IkifOrgCrawler().run()


if __name__ == '__main__':
    main()
