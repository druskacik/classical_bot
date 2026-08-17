import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.americancomposers.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'performances-events')
SOURCE = 'American Composers Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# Some Webflow entries omit their city field even though the named venue is
# unambiguous. These defaults are used only for exact venue names.
VENUE_CITIES = {
    'Wu Tsai Theater, David Geffen Hall, Lincoln Center': 'New York',
    'The DiMenna Center for Classical Music': 'New York',
    'Carnegie Hall': 'New York',
}

STATE_LOCATION_RE = re.compile(
    r'(?:\||\b(?:at|in)\b)\s*([A-Za-z][A-Za-z .\'’/-]*?),\s*([A-Z]{2})(?:\s+\d{5})?\b'
)
ADDRESS_LOCATION_RE = re.compile(
    r'\b([A-Za-z][A-Za-z .\'’/-]*?),\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?\b'
)
VENUE_LINE_RE = re.compile(
    r'(?:^|\n)([^\n|]{2,120}?)\s*\|\s*[A-Za-z][A-Za-z .\'’/-]*?,\s*[A-Z]{2}\b',
    re.MULTILINE,
)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '').replace('\u200d', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(card):
    values = [clean_text(node.get_text(' ', strip=True)) for node in card.select('.when .month')]
    day_node = card.select_one('.when .day')
    if len(values) < 2 or not day_node:
        return None
    try:
        return datetime.strptime(
            f'{values[0]} {clean_text(day_node.get_text())} {values[1]}',
            '%B %d %Y',
        ).date().isoformat()
    except ValueError:
        return None


def parse_time(card):
    node = card.select_one('.when .hour')
    value = clean_text(node.get_text(' ', strip=True) if node else '')
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value.upper(), pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def description_text(card):
    node = card.select_one('.event-description:not(.short)') or card.select_one(
        '.event-description.short'
    )
    return clean_text(node.get_text('\n', strip=True) if node else '') or None


def city_from_card(card, venue, description):
    location = card.select_one('.performance-location_wrapper')
    if location:
        parts = [clean_text(node.get_text(' ', strip=True)) for node in location.find_all('div', recursive=False)]
        parts = [part for part in parts if part and part != ',']
        if parts:
            return parts[0]

    searchable = description or ''
    for pattern in (STATE_LOCATION_RE, ADDRESS_LOCATION_RE):
        matches = list(pattern.finditer(searchable))
        if matches:
            return clean_text(matches[-1].group(1))
    return VENUE_CITIES.get(venue)


def venue_from_description(description):
    match = VENUE_LINE_RE.search(description or '')
    if not match:
        return ''
    venue = clean_text(match.group(1))
    return '' if venue.lower() in {'venue tbd', 'tbd'} else venue


def parse_card(card):
    title_node = card.select_one('.heading-event')
    link = card.select_one('.event-title a[href]')
    venue_node = card.select_one('.event-location')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    event_date = parse_date(card)
    description = description_text(card)
    venue = clean_text(venue_node.get_text(' ', strip=True) if venue_node else '')
    venue = venue or venue_from_description(description)
    city = city_from_card(card, venue, description)
    url = urljoin(EVENTS_URL, link.get('href')) if link else ''

    if not all((title, event_date, url, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(card),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(EVENTS_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    # Webflow adds tab-panel ARIA roles in JavaScript, but the CMS cards are
    # present in the original document and retain this stable collection class.
    cards = soup.select('[role="listitem"].p-e-item')
    records = []
    skipped = 0
    for card in cards:
        record = parse_card(card)
        if record:
            records.append(record)
        else:
            skipped += 1

    if skipped:
        log_message(
            'Skipped event cards missing required fields',
            event='crawler_records_skipped',
            level='warning',
            url=EVENTS_URL,
            record_count=skipped,
        )
    if not records:
        log_message(
            'No valid event cards found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class AmericanComposersOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='americancomposers_org',
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
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_events()


def main():
    AmericanComposersOrgCrawler().run()


if __name__ == '__main__':
    main()
