import html
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.robertoprosseda.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendario')
ACCESS_TOKENS_URL = urljoin(SOURCE_URL, '_api/v1/access-tokens')
EVENTS_API_URL = urljoin(
    SOURCE_URL, '_api/wix-one-events-server/web/paginated-events/viewer'
)
EVENTS_APP_ID = '140603ad-af8d-84a5-2c80-a0f60cb47351'
SOURCE = 'Roberto Prosseda'
PAGE_SIZE = 100

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
    'Referer': SOURCE_URL,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_access_token(session):
    response = session.get(ACCESS_TOKENS_URL, timeout=45)
    response.raise_for_status()
    app = response.json().get('apps', {}).get(EVENTS_APP_ID, {})
    token = app.get('accessToken') or app.get('instance')
    if not token:
        raise ValueError('Wix Events access token was not present in the bootstrap response')
    return token


def get_event_pages(session):
    session.headers['Authorization'] = get_access_token(session)
    offset = 0
    while True:
        parameters = {
            'offset': offset,
            'filter': 0,
            'byEventId': 'false',
            'members': 'true',
            'paidPlans': 'false',
            'locale': 'it-it',
            'filterType': 1,
            'sortOrder': 0,
            'limit': PAGE_SIZE,
            'fetchBadges': 'true',
            'draft': 'false',
            'compId': 'comp-le2t773t',
        }
        response = session.get(EVENTS_API_URL, params=parameters, timeout=45)
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events') or []
        yield events, (payload.get('dates') or {}).get('events', {})

        offset += len(events)
        if not payload.get('hasMore'):
            break
        if not events:
            raise ValueError('Wix Events pagination reported more results but returned no events')


def location_fields(event):
    location = event.get('location') or {}
    address = location.get('fullAddress') or {}
    city = clean_text(address.get('city'))
    country_code = clean_text(address.get('country')).upper()
    venue = clean_text(location.get('name'))

    # Older Wix geocodes occasionally omit structured fields even though the
    # displayed address and event title identify the place unambiguously.
    formatted_address = clean_text(address.get('formattedAddress') or location.get('address'))
    title = clean_text(event.get('title'))
    if not city and re.search(r'\bPatmos\b|\bPatmo\b', f'{title} {formatted_address}', re.I):
        city = 'Patmos'
    if not city and re.search(r'\bMilano\b', f'{title} {formatted_address}', re.I):
        city = 'Milano'
    if not country_code and re.search(r'\bItalia\b', formatted_address, re.I):
        country_code = 'IT'

    if location.get('tbd') or venue.casefold() in {'luogo da definire', 'tbd'}:
        return None
    # A city-only location is not a defensible venue. The record is skipped
    # unless Wix supplies a distinct venue name.
    if not venue or venue.casefold() == city.casefold():
        return None
    if not city or not re.fullmatch(r'[A-Z]{2}', country_code):
        return None
    return venue, city, country_code


def parse_event(event, date_data):
    title = clean_text(event.get('title'))
    slug = clean_text(event.get('slug'))
    local_start = clean_text(date_data.get('startDateISOFormatNotUTC'))
    location = location_fields(event)
    if not title or not slug or not location or not re.match(r'^\d{4}-\d{2}-\d{2}T', local_start):
        return None

    description_parts = [
        clean_text(event.get('description')),
        clean_text(event.get('about')),
    ]
    description = '\n\n'.join(part for part in description_parts if part) or None
    venue, city, country_code = location
    return {
        'title': title,
        'date': local_start[:10],
        'url': urljoin(SOURCE_URL, f'event-details/{slug}'),
        'time_from': local_start[11:16],
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
    }


class RobertoProssedaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='robertoprosseda_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        try:
            for events, dates in get_event_pages(session):
                for event in events:
                    record = parse_event(event, dates.get(event.get('id'), {}))
                    if record:
                        records.append(record)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Roberto Prosseda events',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    RobertoProssedaComCrawler().run()


if __name__ == '__main__':
    main()
