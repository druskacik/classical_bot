import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.waxahachiesymphony.org/'
SOURCE = 'Waxahachie Symphony Association'
ACCESS_TOKENS_URL = f'{SOURCE_URL}_api/v1/access-tokens'
EVENTS_API_URL = f'{SOURCE_URL}_api/wix-one-events-server/web/paginated-events/viewer'
EVENTS_APP_ID = '140603ad-af8d-84a5-2c80-a0f60cb47351'
PAGE_SIZE = 100

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
    text = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True) if '<' in raw else raw
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event(event, dates):
    event_id = clean_text(event.get('id'))
    slug = clean_text(event.get('slug'))
    title = clean_text(event.get('title'))
    date_info = dates.get(event_id) or {}
    start = clean_text(date_info.get('startDateISOFormatNotUTC'))
    location = event.get('location') or {}
    address = location.get('fullAddress') or {}
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('city'))

    try:
        start_datetime = datetime.fromisoformat(start)
        event_date = start_datetime.date().isoformat()
        time_from = start_datetime.strftime('%H:%M')
    except (TypeError, ValueError):
        event_date = ''
        time_from = None

    if not all((title, event_date, slug, venue, city)):
        return None

    description_parts = []
    for field in ('description', 'about'):
        text = clean_text(event.get(field))
        if text and text not in description_parts:
            description_parts.append(text)

    return {
        'title': title,
        'date': event_date,
        'url': f'{SOURCE_URL}event-details-registration/{slug}',
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class WaxahachieSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='waxahachiesymphony_org',
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
        token_response = session.get(ACCESS_TOKENS_URL, headers=HEADERS, timeout=45)
        token_response.raise_for_status()
        token = token_response.json()['apps'][EVENTS_APP_ID]['instance']

        records = []
        offset = 0
        while True:
            response = session.get(
                EVENTS_API_URL,
                params={
                    'offset': offset,
                    'limit': PAGE_SIZE,
                    'locale': 'en-us',
                    'filterType': 1,
                    'sortOrder': 0,
                    'draft': 'false',
                },
                headers={**HEADERS, 'Authorization': token, 'x-wix-brand': 'wix'},
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
            events = payload.get('events') or []
            dates = ((payload.get('dates') or {}).get('events') or {})

            for event in events:
                record = parse_event(event, dates)
                if record:
                    records.append(record)
                else:
                    slug = clean_text(event.get('slug'))
                    log_message(
                        'Skipped incomplete Waxahachie Symphony Association event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=f'{SOURCE_URL}event-details-registration/{slug}',
                        error_type='IncompleteEventData',
                        error_message='Required title, date, URL slug, venue, or city is missing',
                    )

            if not payload.get('hasMore') or not events:
                break
            offset += len(events)

        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    WaxahachieSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
