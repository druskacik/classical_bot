import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.riversidephilharmonic.org/'
SOURCE = 'Riverside Philharmonic'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP]M)\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_date(value):
    match = DATE_RE.search(value)
    if not match:
        return None
    month, day, year = match.groups()
    try:
        return datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    try:
        return datetime.strptime(
            f'{hour}:{minute or "00"} {meridiem.upper()}', '%I:%M %p'
        ).strftime('%H:%M')
    except ValueError:
        return None


def event_location(value):
    lowered = value.lower()
    if 'elliott duchon concert hall' in lowered:
        return 'Elliott Duchon Concert Hall', 'Jurupa Valley'
    if 'rubidoux highschool' in lowered or 'rubidoux high school' in lowered:
        return 'Rubidoux High School', 'Jurupa Valley'
    if 'coil theatre' in lowered:
        return 'Coil Theatre', 'Riverside'
    return None, None


def detailed_descriptions(soup):
    descriptions = {}
    for heading in soup.select('h1, h2, h3, h4'):
        title = clean_text(heading.get_text(' ', strip=True))
        if not title:
            continue
        container = heading.find_parent(class_='sqs-html-content')
        if not container:
            continue
        body = clean_text(container.get_text(' ', strip=True))
        if DATE_RE.search(body) and len(body) > len(title) + 100:
            descriptions[re.sub(r'\s*\(NEW!\)\s*$', '', title, flags=re.I)] = body
    return descriptions


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(SOURCE_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    details = detailed_descriptions(soup)

    records = []
    seen = set()
    for card in soup.select('li.list-item'):
        title_node = card.select_one('.list-item-content__title')
        if not title_node:
            continue
        title = clean_text(title_node.get_text(' ', strip=True))
        card_text = clean_text(card.get_text(' ', strip=True))
        event_date = parse_date(card_text)
        venue, city = event_location(card_text)
        if not title or not event_date or not venue or not city:
            continue

        key = (title, event_date, parse_time(card_text), venue)
        if key in seen:
            continue
        seen.add(key)
        detail_key = re.sub(r'\s*\(NEW!\)\s*$', '', title, flags=re.I)
        records.append({
            'title': title,
            'date': event_date,
            'url': SOURCE_URL,
            'time_from': key[2],
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': details.get(detail_key) or card_text,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    if not records:
        log_message(
            'No parseable concert cards found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class RiversidePhilharmonicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='riversidephilharmonic_org',
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
        return scrape_concerts()


def main():
    RiversidePhilharmonicOrgCrawler().run()


if __name__ == '__main__':
    main()
