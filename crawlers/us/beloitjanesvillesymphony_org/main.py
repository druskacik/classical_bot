import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://beloitjanesvillesymphony.org/'
SOURCE = 'Beloit Janesville Symphony Orchestra'
UPCOMING_VENUE = 'Blackhawk Technical College'
UPCOMING_CITY = 'Janesville'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(r'\b(\d{1,2}/\d{1,2}/\d{4})\b')
TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?)\s*([AP]M)\b', re.IGNORECASE)


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%m/%d/%Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(
            f'{match.group(1)} {match.group(2).upper()}',
            '%I:%M %p' if ':' in match.group(1) else '%I %p',
        ).strftime('%H:%M')
    except ValueError:
        return None


def parse_location(value):
    text = clean_text(value)
    city_match = re.search(r'\b(Beloit|Janesville)\s*,\s*WI\b', text, re.IGNORECASE)
    if not city_match:
        return None
    city = city_match.group(1).title()
    venue = re.split(r'\s+\d{2,6}\s+', text, maxsplit=1)[0].strip(' ,')
    venue = re.sub(r'\s+', ' ', venue)
    if not venue:
        return None
    return venue, city


def calendar_records(soup):
    records = []
    for card in soup.select('[data-aid="CALENDAR_BIGGER_SCREEN_CONTAINER"]'):
        title = clean_text(card.select_one('[data-aid="CALENDAR_EVENT_TITLE"]'))
        event_date = parse_date(card.select_one('[data-aid="CALENDAR_EVENT_DATE"]'))
        time_from = parse_time(card.select_one('[data-aid="CALENDAR_EVENT_TIME"]'))
        time_node = card.select_one('[data-aid="CALENDAR_EVENT_TIME"]')
        location_node = time_node.find_next_sibling('p') if time_node else None
        location = parse_location(location_node)
        if not title or not event_date or not location:
            continue
        venue, city = location
        description = clean_text(card.select_one('[data-aid="CALENDAR_DESC_TEXT"]')) or None
        records.append({
            'title': title,
            'date': event_date,
            'url': f'{SOURCE_URL}#concert-{event_date}',
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def upcoming_records(soup):
    heading = next(
        (node for node in soup.find_all(['h1', 'h2', 'h3'])
         if clean_text(node).lower() == 'upcoming concerts'),
        None,
    )
    if heading is None:
        return []

    container = heading.parent.parent
    text = clean_text(container)
    pattern = re.compile(
        r'(?:Sunday:\s*)?(\d{1,2}/\d{1,2}/\d{4})\s*'
        r'(.*?)\s*(\([^)]*Conductor[^)]*\))\s*'
        r'(\d{1,2}(?::\d{2})?\s*[AP]M)\s*\|\s*Blackhawk Technical College',
        re.IGNORECASE,
    )
    records = []
    for match in pattern.finditer(text):
        event_date = parse_date(match.group(1))
        if not event_date:
            continue
        supplied_title = clean_text(match.group(2)).strip(' -–—')
        title = supplied_title or f'{SOURCE} Concert'
        records.append({
            'title': title,
            'date': event_date,
            'url': f'{SOURCE_URL}#upcoming-{event_date}',
            'time_from': parse_time(match.group(4)),
            'venue': UPCOMING_VENUE,
            'city': UPCOMING_CITY,
            'country_code': 'US',
            'description': clean_text(match.group(3)).strip('()') or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class BeloitJanesvilleSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='beloitjanesvillesymphony_org',
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
            response = requests.get(SOURCE_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Beloit Janesville Symphony concerts',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = calendar_records(soup) + upcoming_records(soup)
        if not records:
            log_message(
                'No Beloit Janesville Symphony concerts found',
                event='crawler_empty_listing',
                level='warning',
                url=SOURCE_URL,
                record_count=0,
            )
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    BeloitJanesvilleSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
