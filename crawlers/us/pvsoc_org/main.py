import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.pvsoc.org/'
SOURCE = 'Pioneer Valley Symphony'
ACCESS_TOKENS_URL = f'{SOURCE_URL}_api/v1/access-tokens'
EVENTS_API_URL = f'{SOURCE_URL}_api/wix-one-events-server/web/paginated-events/viewer'
EVENT_DETAIL_API_URL = f'{SOURCE_URL}_api/wix-one-events-server/html/page-data'
WIX_EVENTS_APP_ID = '140603ad-af8d-84a5-2c80-a0f60cb47351'
PAGE_SIZE = 100
LOCAL_TIMEZONE = ZoneInfo('America/New_York')
KNOWN_CITY_BY_VENUE = {
    'Greenfield High School Auditorium': 'Greenfield',
    'John M. Greene Hall, Smith College': 'Northampton',
    'Millside Park': 'Easthampton',
    'Park Hill Orchard': 'Easthampton',
    'Second Congregational Church': 'Greenfield',
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = (
        BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
        if '<' in raw
        else raw.strip()
    )
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_local_start(event, date_info):
    local_start = clean_text(date_info.get('startDateISOFormatNotUTC'))
    if not local_start:
        local_start = clean_text(
            ((event.get('scheduling') or {}).get('config') or {}).get('startDate')
        )
    try:
        parsed = datetime.fromisoformat(local_start.replace('Z', '+00:00'))
    except ValueError:
        return None, None

    if not date_info and parsed.tzinfo is not None:
        parsed = parsed.astimezone(LOCAL_TIMEZONE)

    time_from = None if parsed.hour == parsed.minute == parsed.second == 0 else parsed.strftime('%H:%M')
    return parsed.date().isoformat(), time_from


def parse_event(event, dates):
    event_id = clean_text(event.get('id'))
    title = clean_text(event.get('title'))
    slug = clean_text(event.get('slug'))
    location = event.get('location') or {}
    full_address = location.get('fullAddress') or {}
    venue = clean_text(location.get('name'))
    city = clean_text(full_address.get('city'))
    country_code = clean_text(full_address.get('country')).upper()
    if not city and venue in KNOWN_CITY_BY_VENUE:
        city = KNOWN_CITY_BY_VENUE[venue]
        country_code = 'US'
    event_date, time_from = parse_local_start(event, dates.get(event_id) or {})

    if venue.casefold() == city.casefold():
        venue = ''
    if not all((title, event_date, slug, venue, city, country_code)):
        return None

    description_parts = []
    for field in ('description', 'about'):
        text = clean_text(event.get(field))
        if text and text not in description_parts:
            description_parts.append(text)

    return {
        'title': title,
        'date': event_date,
        'url': f'{SOURCE_URL}event-details/{slug}',
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class PvsocOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='pvsoc_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        token_response = session.get(ACCESS_TOKENS_URL, timeout=45)
        token_response.raise_for_status()
        token = token_response.json()['apps'][WIX_EVENTS_APP_ID]['instance']

        records = []
        offset = 0
        while True:
            response = session.get(
                EVENTS_API_URL,
                params={
                    'offset': offset,
                    'locale': 'en-us',
                    'filterType': 3,
                    'sortOrder': 0,
                    'limit': PAGE_SIZE,
                    'fetchBadges': 'true',
                    'compId': 'comp-lb4aj3ge',
                },
                headers={'Authorization': token, 'x-wix-brand': 'wix'},
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
            events = payload.get('events') or []
            dates = ((payload.get('dates') or {}).get('events') or {})
            for event in events:
                slug = clean_text(event.get('slug'))
                detail_url = f'{EVENT_DETAIL_API_URL}/{slug}'
                if slug:
                    try:
                        detail_response = session.get(
                            detail_url,
                            params={
                                'locale': 'en',
                                'regional': 'en-us',
                                'tz': 'America/New_York',
                            },
                            headers={'Authorization': token, 'x-wix-brand': 'wix'},
                            timeout=45,
                        )
                        detail_response.raise_for_status()
                        event = detail_response.json().get('event') or event
                    except requests.RequestException as error:
                        log_message(
                            'Pioneer Valley Symphony event detail failed',
                            event='crawler_item_failed',
                            level='warning',
                            url=detail_url,
                            error_type=type(error).__name__,
                            error_message=str(error),
                        )
                record = parse_event(event, dates)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Pioneer Valley Symphony event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=f"{SOURCE_URL}event-details/{clean_text(event.get('slug'))}",
                        error_type='IncompleteEventData',
                        error_message='Required date, title, venue, city, country, or slug is missing',
                    )

            if not events or len(events) < PAGE_SIZE:
                break
            offset += len(events)

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    PvsocOrgCrawler().run()


if __name__ == '__main__':
    main()
