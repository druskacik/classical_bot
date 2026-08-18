import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.chathambaroque.org/'
SOURCE = 'Chatham Baroque'
CALENDAR_API = SOURCE_URL + 'wp-json/aot-calendar/v1/events'

# These are the site's performance calendars.  The dated season categories are
# retained because the API continues to expose past performances.
CATEGORIES = ('24-25', '25-26', '26-27', 'pbj')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/html;q=0.9,*/*;q=0.8',
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', unescape(str(value)).replace('\xa0', ' ')).strip()


def calendar_events(session, category):
    response = session.get(
        CALENDAR_API,
        params={
            'upcoming': 0,
            'cpt': 'concert',
            'categories': category,
            'taxonomy': 'category',
            # Explicit wide bounds make the archive independent of today's date.
            'start': '2000-01-01T00:00:00Z',
            'end': '2100-01-01T00:00:00Z',
        },
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(f'Calendar API returned non-list data for {category}')
    return payload


def description_from_page(soup):
    candidates = []
    for node in soup.select('.et_pb_text_inner'):
        text = clean_text(node.get_text(' ', strip=True))
        lowered = text.lower()
        if (
            len(text) >= 80
            and 'other series concerts' not in lowered
            and 'purchase 26/27 season subscription' not in lowered
            and not re.match(r'^(monday|tuesday|wednesday|thursday|friday|saturday|sunday),', lowered)
        ):
            candidates.append(text)
    return max(candidates, key=len) if candidates else None


def venue_for_event(soup, event_datetime):
    month_day = event_datetime.strftime('%B %-d').lower()

    for paragraph in soup.select('p'):
        parts = [clean_text(part) for part in paragraph.stripped_strings]
        parts = [part for part in parts if part]
        if not parts:
            continue
        text = ' '.join(parts).lower()
        if month_day not in text:
            continue

        # Season pages place the venue after the bold date/time. PBJ pages put
        # date, time, and venue in one text node, so handle both representations.
        if len(parts) >= 2:
            venue = parts[-1]
            if not re.search(r'\b(?:AM|PM)\b', venue, flags=re.IGNORECASE):
                return venue
        match = re.search(
            r'\b\d{1,2}:\d{2}\s*[AP]M\s+(.+)$', parts[0], flags=re.IGNORECASE
        )
        if match:
            return clean_text(match.group(1))
    return None


def event_to_record(event, session, page_cache):
    url = event.get('url')
    title = clean_text(event.get('title'))
    start = event.get('start')
    try:
        event_datetime = datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None
    if not title or not url or not url.startswith(SOURCE_URL):
        return None

    if url not in page_cache:
        response = session.get(url, headers=HEADERS, timeout=45)
        response.raise_for_status()
        page_cache[url] = BeautifulSoup(response.text, 'html.parser')
    soup = page_cache[url]
    venue = venue_for_event(soup, event_datetime)
    if not venue:
        log_message(
            'Skipping concert occurrence without a defensible venue',
            event='crawler_record_skipped',
            level='warning',
            url=url,
            event_date=event_datetime.date().isoformat(),
        )
        return None

    return {
        'title': title,
        'date': event_datetime.date().isoformat(),
        'url': url,
        'time_from': event_datetime.strftime('%H:%M'),
        'venue': venue,
        'city': 'Pittsburgh',
        'description': description_from_page(soup),
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    page_cache = {}
    records = []
    seen_event_ids = set()
    events = []

    with ThreadPoolExecutor(max_workers=len(CATEGORIES)) as executor:
        category_results = executor.map(
            lambda category: calendar_events(requests.Session(), category), CATEGORIES
        )
        event_lists = list(category_results)

    for event_list in event_lists:
        for event in event_list:
            event_id = event.get('id')
            if not event_id or event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
            events.append(event)

    urls = {event.get('url') for event in events if event.get('url')}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(requests.get, url, headers=HEADERS, timeout=45): url
            for url in urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                response = future.result()
                response.raise_for_status()
                page_cache[url] = BeautifulSoup(response.text, 'html.parser')
            except requests.RequestException as error:
                log_message(
                    'Concert detail request failed',
                    event='crawler_detail_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for event in events:
        if event.get('url') not in page_cache:
            continue
        record = event_to_record(event, session, page_cache)
        if record:
            records.append(record)

    if not records:
        log_message(
            'No parseable performances found in Chatham Baroque calendars',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_API,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class ChathamBaroqueOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chathambaroque_org',
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
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ChathamBaroqueOrgCrawler().run()


if __name__ == '__main__':
    main()
