import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wssymphony.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events/list/')
SOURCE = 'Winston-Salem Symphony'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

TIME_RE = re.compile(r'\b(\d{1,2}):(\d{2})\s*([ap])\.?m\.?\b', re.IGNORECASE)
STATE_NAMES = {'NC', 'North Carolina'}


def clean_text(value):
    if not value:
        return ''
    value = str(value).replace('\xa0', ' ').replace('\u202f', ' ')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    value = re.sub(r'\n{3,}', '\n\n', value)
    return value.strip()


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour < 1 or hour > 12 or minute > 59:
        return None
    if match.group(3).lower() == 'p' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'a' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def parse_city(address):
    parts = [clean_text(part) for part in clean_text(address).split(',') if clean_text(part)]
    for index, part in enumerate(parts):
        if part in STATE_NAMES and index:
            return parts[index - 1]
    return None


def parse_event(row):
    title_link = row.select_one('.tribe-events-calendar-list__event-title-link[href]')
    date_tag = row.select_one('.tribe-events-calendar-list__event-date-tag-datetime[datetime]')
    datetime_tag = row.select_one('.tribe-events-calendar-list__event-datetime')
    venue_tag = row.select_one('.tribe-events-calendar-list__event-venue-title')
    address_tag = row.select_one('.tribe-events-calendar-list__event-venue-address')

    title = clean_text(title_link.get_text(' ', strip=True)) if title_link else ''
    url = urljoin(EVENTS_URL, title_link.get('href')) if title_link else ''
    venue = clean_text(venue_tag.get_text(' ', strip=True)) if venue_tag else ''
    address = clean_text(address_tag.get_text(' ', strip=True)) if address_tag else ''
    city = parse_city(address)
    raw_date = date_tag.get('datetime', '')[:10] if date_tag else ''
    try:
        event_date = date.fromisoformat(raw_date).isoformat()
    except ValueError:
        event_date = None

    if not all((title, url, event_date, venue, city)):
        return None

    description_tag = row.select_one('.tribe-events-calendar-list__event-description')
    description = (
        clean_text(description_tag.get_text('\n', strip=True)) if description_tag else None
    ) or None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(datetime_tag.get_text(' ', strip=True)) if datetime_tag else None,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_soup(session, url):
    response = session.get(url, timeout=40)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    feed_urls = [
        f'{EVENTS_URL}?posts_per_page=1000',
        f'{EVENTS_URL}?posts_per_page=1000&eventDisplay=past',
    ]
    records = []
    seen = set()

    for feed_url in feed_urls:
        soup = get_soup(session, feed_url)
        for row in soup.select('.tribe-events-calendar-list__event-row'):
            record = parse_event(row)
            if not record:
                link = row.select_one('.tribe-events-calendar-list__event-title-link[href]')
                log_message(
                    'Skipping event without a valid date, venue, or city',
                    event='crawler_item_skipped',
                    level='warning',
                    url=urljoin(EVENTS_URL, link.get('href')) if link else feed_url,
                )
                continue
            key = (record['url'], record['date'], record['time_from'], record['venue'])
            if key not in seen:
                seen.add(key)
                records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class WssymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wssymphony_org',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    WssymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
