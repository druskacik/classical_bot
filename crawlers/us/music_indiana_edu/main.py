import html
import re
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://music.indiana.edu/'
SOURCE = 'Indiana University Jacobs School of Music'
API_URL = (
    'https://events.iu.edu/live/json/events/'
    'group/Jacobs%20School%20of%20Music/'
    'start_date/{start_date}/end_date/{end_date}/'
    'response_fields/event_types,location,tags,summary,description,group_title'
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) HeadlessChrome/151.0.0.0 Safari/537.36'
    ),
}

CITY_RULES = (
    (re.compile(r'\bBloomington\b', re.IGNORECASE), 'Bloomington', 'US'),
    (re.compile(r'\bIndianapolis\b', re.IGNORECASE), 'Indianapolis', 'US'),
    (re.compile(r'\bHuntingburg\b', re.IGNORECASE), 'Huntingburg', 'US'),
    (re.compile(r'\bShoals\b', re.IGNORECASE), 'Shoals', 'US'),
    (re.compile(r'\bNashville\b', re.IGNORECASE), 'Nashville', 'US'),
    (re.compile(r'\bSalem\b', re.IGNORECASE), 'Salem', 'US'),
    (re.compile(r'\bBeijing\b', re.IGNORECASE), 'Beijing', 'CN'),
    (re.compile(r'\bSeoul\b', re.IGNORECASE), 'Seoul', 'KR'),
)


def clean_html(value):
    if not value:
        return None
    value = str(value)
    text = (
        BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
        if '<' in value
        else value
    )
    text = html.unescape(text).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip() or None


def description_from_event(event):
    parts = []
    for value in (event.get('summary'), event.get('description')):
        text = clean_html(value)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def geography_from_event(event, venue):
    for pattern, city, country_code in CITY_RULES:
        if pattern.search(venue):
            return city, country_code

    # This is the calendar of a Bloomington-based school and its ordinary
    # campus venues are all in Bloomington. Explicit touring cities above take
    # precedence, including the retained international recital occurrences.
    return 'Bloomington', 'US'


def parse_event(event):
    title = clean_html(event.get('title'))
    url = clean_html(event.get('url'))
    venue = clean_html(event.get('location'))

    tags = {clean_html(tag) for tag in event.get('tags') or []}
    if not venue and (event.get('is_online') or tags.intersection({'Online', 'Virtual'})):
        venue = 'Online'

    date_iso = event.get('date_iso')
    if not title or not url or not venue or not date_iso:
        return None

    try:
        start = datetime.fromisoformat(date_iso)
    except (TypeError, ValueError):
        return None

    city, country_code = geography_from_event(event, venue)
    time_from = None if event.get('is_all_day') else start.strftime('%H:%M')

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description_from_event(event),
    }


class MusicIndianaEduCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='music_indiana_edu',
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
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'url'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def fetch_page(self, session, start_date, end_date, page):
        url = API_URL.format(start_date=start_date, end_date=end_date)
        try:
            response = session.get(url, params={'page': page}, timeout=60)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Jacobs School of Music events',
                event='crawler_fetch_failed',
                level='error',
                url=url,
                page=page,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        if not isinstance(payload.get('data'), list):
            raise ValueError('Jacobs School events API returned invalid event data')
        return payload

    def fetch_range(self, session, start_date, end_date):
        payload = self.fetch_page(session, start_date, end_date, 1)
        metadata = payload.get('meta') or {}
        total_results = int(metadata.get('total_results') or 0)

        # LiveWhale caps broad result sets at roughly 1,000 occurrences. Split
        # dense ranges before paging so the later dates are not silently lost.
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        if total_results >= 900 and start < end:
            midpoint = start + (end - start) // 2
            return self.fetch_range(session, start.isoformat(), midpoint.isoformat()) + self.fetch_range(
                session, (midpoint + timedelta(days=1)).isoformat(), end.isoformat()
            )

        events = list(payload['data'])
        total_pages = int(metadata.get('total_pages') or 1)
        for page in range(2, total_pages + 1):
            events.extend(self.fetch_page(session, start_date, end_date, page)['data'])
        return events

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        events = self.fetch_range(session, '2000-01-01', '2100-12-31')
        records = []

        for event in events:
            record = parse_event(event)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    MusicIndianaEduCrawler().run()


if __name__ == '__main__':
    main()
