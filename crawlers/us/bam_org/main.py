import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bam.org/'
SOURCE = 'Brooklyn Academy of Music (BAM)'
CALENDAR_API = urljoin(SOURCE_URL, 'api/BAMApi/GetCalendarEventsByDayWithOnGoing')
ARCHIVE_START_YEAR = 2021
CITY = 'Brooklyn'

# BAM is a mixed arts center. These first-party disciplines form a deliberately
# broad candidate feed; the potential-event classifier makes the final scope
# decision. Film-only, talks-only, community-only, and class listings are omitted.
CANDIDATE_GENRES = {
    'Music',
    'Opera',
    'Dance',
    'Theater',
    'Performance Art',
    'New Media',
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/html;q=0.9',
    'Accept-Language': 'en-US,en;q=0.8',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def is_candidate(event):
    genres = {clean_text(value) for value in (event.get('genres') or '').split(',')}
    return bool(genres & CANDIDATE_GENRES)


def get_calendar_events(session):
    events = []
    for year in range(ARCHIVE_START_YEAR, date.today().year + 2):
        response = session.get(
            CALENDAR_API,
            params={'start': f'{year}-01-01', 'end': f'{year}-12-31'},
            timeout=90,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError(f'BAM calendar API returned an unexpected response for {year}')
        events.extend(event for event in payload if is_candidate(event))
    return events


def jsonld_events(soup):
    events = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        nodes = payload.get('graph', []) if isinstance(payload, dict) else []
        events.extend(node for node in nodes if node.get('@type') == 'Event')
    return events


def detail_fields(html):
    soup = BeautifulSoup(html, 'html.parser')

    description = ''
    content = soup.select_one('.production-related-content .description')
    if content:
        description = clean_text(content)
        if description.casefold().startswith('leadership support'):
            description = ''
    if not description:
        meta = soup.select_one('meta[name="description"]')
        description = clean_text(meta.get('content')) if meta else ''

    venue = ''
    venue_heading = next(
        (heading for heading in soup.find_all(['h2', 'h3']) if clean_text(heading.get_text()) == 'VENUE'),
        None,
    )
    if venue_heading:
        block = venue_heading.find_next_sibling()
        if block:
            venue_parts = [clean_text(link.get_text()) for link in block.find_all('a')]
            venue_parts = [part for part in venue_parts if part]
            venue = ' – '.join(dict.fromkeys(venue_parts)) or clean_text(block)

    locality = ''
    for event in jsonld_events(soup):
        address = (event.get('location') or {}).get('address') or {}
        locality = clean_text(address.get('addressLocality'))
        if locality:
            break
    return description or None, venue, locality


def valid_venue(venue):
    normalized = venue.casefold()
    invalid_markers = ('virtual', 'online', 'various', 'to be announced', 'tba')
    return bool(venue) and not any(marker in normalized for marker in invalid_markers)


def detail_for_url(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return detail_fields(response.text)


def make_records(events, details):
    records = []
    for event in events:
        title = clean_text(event.get('name'))
        path = clean_text(event.get('moreLink'))
        url = urljoin(SOURCE_URL, path)
        description, venue, locality = details.get(url, (None, '', ''))
        summary = clean_text(event.get('desc'))
        if summary and description and summary not in description:
            description = f'{summary}\n\n{description}'
        else:
            description = description or summary or None
        city = locality or CITY
        if not title or not path or not valid_venue(venue) or not city:
            continue

        for value in event.get('performances') or []:
            match = re.match(r'^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})', value or '')
            if not match:
                continue
            try:
                event_date = date.fromisoformat(match.group(1)).isoformat()
            except ValueError:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': f'{match.group(2)}:{match.group(3)}',
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    unique = {
        (item['title'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    return sorted(
        unique.values(),
        key=lambda item: (item['date'], item['time_from'], item['title'], item['venue']),
    )


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = get_calendar_events(session)
    urls = sorted({urljoin(SOURCE_URL, event.get('moreLink', '')) for event in events if event.get('moreLink')})
    details = {}

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(detail_for_url, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                details[url] = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape BAM event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = make_records(events, details)
    if not records:
        log_message(
            'No BAM candidate performances found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_API,
            record_count=0,
        )
    return records


class BamOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bam_org',
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
        return scrape_concerts()


def main():
    BamOrgCrawler().run()


if __name__ == '__main__':
    main()
