import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://music.rice.edu/'
LISTING_URL = urljoin(SOURCE_URL, 'events')
SOURCE = 'The Shepherd School of Music'
CITY = 'Houston'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)).strip()


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), '%a, %b %d, %Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(value):
    text = clean_text(value).replace('.', '').upper()
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(text, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_upcoming_date(value):
    text = clean_text(value)
    for year in (date.today().year, date.today().year + 1):
        try:
            parsed = datetime.strptime(f'{text}, {year}', '%a, %b %d, %Y').date()
        except ValueError:
            return ''
        if parsed >= date.today():
            return parsed.isoformat()
    return ''


def event_entries(session):
    entries = []
    seen = set()
    for page in range(100):
        response = session.get(LISTING_URL, params={'page': page} if page else None, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        page_entries = []
        for divider in soup.select('.views-infinite-scroll-content-wrapper .divider'):
            date_node = divider.find('h3', recursive=False)
            for event in divider.select('.event'):
                link = event.select_one('a.event-wrapper-link[href]')
                if not link:
                    continue
                entry = {
                    'url': urljoin(LISTING_URL, link.get('href')),
                    'date': parse_upcoming_date(date_node),
                    'time_from': parse_time(event.select_one('.event-time')),
                    'venue': clean_text(event.select_one('.event-location')),
                }
                key = (entry['url'], entry['date'], entry['time_from'], entry['venue'])
                if key not in seen:
                    page_entries.append(entry)
                    seen.add(key)
        if not page_entries:
            break
        entries.extend(page_entries)

        next_link = soup.select_one('.pager__item--next a[href], a[rel="next"]')
        if not next_link:
            break
    else:
        log_message(
            'Event listing reached pagination safety limit',
            event='crawler_pagination_limit',
            level='warning',
            url=LISTING_URL,
            record_count=len(entries),
        )
    return entries


def parse_event(entry):
    url = entry['url']
    title = event_date = venue = ''
    soup = None
    for _attempt in range(3):
        response = requests.get(url, headers=HEADERS, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        title = clean_text(soup.select_one('main h1'))
        event_date = parse_date(soup.select_one('.event-sidebar .event-date, aside .event-date'))
        venue = clean_text(soup.select_one('.event-sidebar .event-location, aside .event-location'))
        if title and venue and (event_date or entry['date']):
            break
    has_dated_detail = bool(event_date)
    event_date = event_date or entry['date']
    venue = venue or entry['venue']
    if not title or not event_date or not venue:
        log_message(
            'Skipping event with incomplete required fields',
            event='crawler_event_skipped',
            level='warning',
            url=url,
            error_type='IncompleteEvent',
        )
        return None

    article = soup.select_one('article.node--type-event')
    description = clean_text(article) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': (
            parse_time(soup.select_one('.event-sidebar .event-time, aside .event-time'))
            if has_dated_detail else entry['time_from']
        ),
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    entries = event_entries(session)
    records = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(parse_event, entry): entry['url'] for entry in entries}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Event request failed',
                    event='crawler_event_request_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    if not records:
        log_message(
            'No event records found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class MusicRiceEduCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='music_rice_edu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    MusicRiceEduCrawler().run()


if __name__ == '__main__':
    main()
