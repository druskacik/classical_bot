import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://fishercenter.bard.edu/'
SOURCE = 'Fisher Center at Bard'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
ARCHIVE_START = '1900-01-01 00:00:00'
ARCHIVE_END = '2100-12-31 23:59:59'
PER_PAGE = 50
DETAIL_WORKERS = 8
HEADERS = {
    'Accept': 'application/json',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}
HOME_VENUE_MARKERS = {
    'bard farm', 'blithewood', 'campus center', 'ccs bard', 'fisher center',
    'jim ottaway', 'manor house', 'maple trees on manor avenue',
    'montgomery place', 'olin hall', 'parliament of reality',
    'richard b. fisher center',
}


def clean_text(value):
    if not value:
        return ''
    text = unescape(str(value))
    if '<' in text and '>' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r' *\n+ *', '\n', text).strip()


def country_code_for(venue):
    country = clean_text((venue or {}).get('country')).lower().rstrip('.')
    if not country or country in {'united states', 'united states of america', 'usa', 'us'}:
        return 'US'
    return None


def city_for(venue):
    city = clean_text((venue or {}).get('city'))
    if city:
        return city
    name = clean_text((venue or {}).get('venue'))
    lowered = name.lower()
    if 'red hook' in lowered:
        return 'Red Hook'
    if 'rhinebeck' in lowered and 'saugerties' not in lowered:
        return 'Rhinebeck'
    if 'hudson' in lowered:
        return 'Hudson'
    if any(marker in lowered for marker in HOME_VENUE_MARKERS):
        return 'Annandale-on-Hudson'
    return None


def record_from_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = city_for(venue_data)
    country_code = country_code_for(venue_data)
    details = event.get('start_date_details') or {}
    try:
        event_date = datetime(
            int(details['year']), int(details['month']), int(details['day'])
        ).date().isoformat()
    except (KeyError, TypeError, ValueError):
        return None

    if not all((title, url, venue, city, country_code)):
        return None

    time_from = None
    if not event.get('all_day'):
        try:
            time_from = f"{int(details['hour']):02d}:{int(details['minutes']):02d}"
        except (KeyError, TypeError, ValueError):
            pass

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(url):
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    root = soup.select_one('.tribe-events-single-content-container')
    if not root:
        return None

    parts = []
    for section in root.select(':scope > section.single-event-section'):
        classes = set(section.get('class') or [])
        if classes.intersection({'single-event-overview', 'single-event-basic'}):
            text = clean_text(section)
            if text and text not in parts:
                parts.append(text)
    return '\n\n'.join(parts) or None


class FisherCenterBardEduCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fishercenter_bard_edu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'url'],
    )
    archive_start = ARCHIVE_START
    archive_end = ARCHIVE_END

    def fetch_events(self, session):
        page = 1
        while True:
            response = session.get(
                EVENTS_API_URL,
                params={
                    'start_date': self.archive_start,
                    'end_date': self.archive_end,
                    'status': 'publish',
                    'per_page': PER_PAGE,
                    'page': page,
                },
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
            events = payload.get('events') or []
            yield from events
            total_pages = int(payload.get('total_pages') or 0)
            if not events or page >= total_pages:
                break
            page += 1

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            records = [
                record for event in self.fetch_events(session)
                if (record := record_from_event(event))
            ]
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Fisher Center event API',
                event='crawler_page_failed',
                level='error',
                url=EVENTS_API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        missing = [record for record in records if not record['description']]
        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as executor:
            futures = {executor.submit(detail_description, record['url']): record for record in missing}
            for future in as_completed(futures):
                record = futures[future]
                try:
                    record['description'] = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to fetch Fisher Center event detail',
                        event='crawler_detail_failed',
                        level='warning',
                        url=record['url'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        unique = {
            (item['title'], item['date'], item['time_from'], item['venue'], item['url']): item
            for item in records
        }
        result = sorted(unique.values(), key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))
        if not result:
            log_message(
                'No valid Fisher Center events found',
                event='crawler_empty_listing',
                level='warning',
                url=EVENTS_API_URL,
                record_count=0,
            )
        return result


def main():
    FisherCenterBardEduCrawler().run()


if __name__ == '__main__':
    main()
