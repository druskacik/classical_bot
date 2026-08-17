import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.valleysymphony.org/'
CONCERTS_URL = f'{SOURCE_URL}concert-and-events/concerts'
SOURCE = 'Valley Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'\b(?:MON|TUE|WED|THU|FRI|SAT|SUN)[A-Z]*,?\s+'
    r'(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)'
    r'\s+(\d{1,2}),\s+(20\d{2})\s*[-–]\s*'
    r'(\d{1,2})(?::([0-5]\d))?\s*([AP])\.?M\.?',
    re.IGNORECASE,
)

VENUES = {
    'mcallen performing arts center': ('McAllen Performing Arts Center', 'McAllen'),
}


def clean_text(node):
    if not node:
        return ''
    text = node.get_text('\n', strip=True) if hasattr(node, 'get_text') else str(node)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(text):
    match = DATE_TIME_RE.search(text)
    if not match:
        return None, None
    month, day, year, hour, minute, meridiem = match.groups()
    try:
        event_date = datetime.strptime(
            f'{month.title()} {day} {year}', '%B %d %Y'
        ).date().isoformat()
    except ValueError:
        return None, None
    hour = int(hour) % 12 + (12 if meridiem.lower() == 'p' else 0)
    return event_date, f'{hour:02d}:{int(minute or 0):02d}'


def find_venue(text):
    normalized = text.casefold()
    for label, venue_data in VENUES.items():
        if label in normalized:
            return venue_data
    return None, None


def parse_concerts(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    records = []

    # Duda stores each concert's complete title/date/venue/programme in one
    # paragraph widget. Selecting those widgets avoids navigation and footer
    # text while preserving the repertoire needed by programme extraction.
    for widget in soup.select('div.dmNewParagraph[data-element-type="paragraph"]'):
        description = clean_text(widget)
        match = DATE_TIME_RE.search(description)
        if not match:
            continue
        event_date, time_from = parse_datetime(description)
        venue, city = find_venue(description)
        title = description[:match.start()].strip(' \n-–')
        if not title or not event_date or not venue or not city:
            log_message(
                'Skipping Valley Symphony concert with incomplete required fields',
                event='crawler_event_skipped',
                level='warning',
                url=CONCERTS_URL,
                has_title=bool(title),
                has_date=bool(event_date),
                has_venue=bool(venue),
                has_city=bool(city),
            )
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': CONCERTS_URL,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class ValleyOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='valleyorchestra_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(CONCERTS_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Valley Symphony concerts',
                event='crawler_fetch_failed',
                level='error',
                url=CONCERTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        return parse_concerts(response.text)


def main():
    ValleyOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
