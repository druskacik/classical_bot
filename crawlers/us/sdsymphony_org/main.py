import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sdsymphony.org/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts-tickets/all-concerts/')
SOURCE = 'South Dakota Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'([A-Z][a-z]+)\s+(\d{1,2})(?:\s*&\s*(\d{1,2}))?,\s*(\d{4})'
    r'\s*\|\s*(\d{1,2}(?::\d{2})?\s*[AP]M)',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_occurrences(value):
    occurrences = []
    for match in DATE_TIME_RE.finditer(clean_text(value)):
        month, first_day, second_day, year, time_value = match.groups()
        for day in (first_day, second_day):
            if not day:
                continue
            try:
                event_date = datetime.strptime(
                    f'{month} {day} {year}', '%B %d %Y'
                ).date().isoformat()
                event_time = datetime.strptime(
                    time_value.replace(' ', '').upper(), '%I:%M%p'
                ).strftime('%H:%M')
            except ValueError:
                continue
            occurrence = (event_date, event_time)
            if occurrence not in occurrences:
                occurrences.append(occurrence)
    return occurrences


def city_for_venue(venue):
    normalized = venue.lower()
    if 'tea area high school' in normalized:
        return 'Tea'
    if any(name in normalized for name in (
        'mary w. sommervold hall',
        'hamre hall',
        'hamre recital hall',
        'first lutheran church',
    )):
        return 'Sioux Falls'
    return None


def detail_description(session, url):
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Could not fetch concert detail',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    soup = BeautifulSoup(response.text, 'html.parser')
    parts = []
    for block in soup.select('main .usn_cmp_text .text'):
        text = clean_text(block)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def listing_cards(session):
    page = 1
    seen_urls = set()
    cards = []

    while True:
        url = LISTING_URL if page == 1 else f'{LISTING_URL}?page={page}'
        response = session.get(url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        page_cards = soup.select('.listing .item-blog')
        new_count = 0

        for card in page_cards:
            link = card.select_one('a[href*="/concerts-tickets/all-concerts/"]')
            if not link:
                continue
            event_url = urljoin(SOURCE_URL, link.get('href', ''))
            if event_url in seen_urls:
                continue
            seen_urls.add(event_url)
            cards.append((card, event_url))
            new_count += 1

        next_link = soup.select_one('.pagination .next a[href], a[aria-label="Next"][href]')
        if not next_link or not page_cards or new_count == 0:
            break
        page += 1

    return cards


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []

    for card, event_url in listing_cards(session):
        link = card.select_one('a[href]')
        lines = [clean_text(value) for value in link.stripped_strings] if link else []
        lines = [value for value in lines if value]
        if len(lines) < 3:
            continue

        date_text, title, venue = lines[:3]
        city = city_for_venue(venue)
        occurrences = parse_occurrences(date_text)
        if not title or not venue or not city or not occurrences:
            log_message(
                'Skipping incomplete concert card',
                event='crawler_record_skipped',
                level='warning',
                url=event_url,
            )
            continue

        description = detail_description(session, event_url)
        for event_date, event_time in occurrences:
            records.append({
                'title': title,
                'date': event_date,
                'url': event_url,
                'time_from': event_time,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class SdSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sdsymphony_org',
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
    SdSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
