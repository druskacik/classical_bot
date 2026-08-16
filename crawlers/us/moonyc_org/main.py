import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://moonyc.org/'
SOURCE = 'Modus Operandi Orchestra'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_CODES = {
    'canada': 'CA',
    'oman': 'OM',
    'united states': 'US',
    'united states of america': 'US',
    'usa': 'US',
}


def clean_text(value):
    if not value:
        return ''
    text = html.unescape(str(value))
    if re.search(r'<[^>]+>', text):
        text = BeautifulSoup(text, 'html.parser').get_text('\n')
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session


def parse_datetime(value):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def country_code_for(venue):
    country = clean_text(venue.get('country')).lower()
    if country:
        return COUNTRY_CODES.get(country)

    city = clean_text(venue.get('city')).lower()
    if city == 'ottawa':
        return 'CA'
    if city == 'muscat':
        return 'OM'
    return 'US'


def parse_event(event):
    title = clean_text(event.get('title'))
    event_date, time_from = parse_datetime(event.get('start_date'))
    url = clean_text(event.get('url'))
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    country_code = country_code_for(venue_data)

    if not all((title, event_date, url, venue, city, country_code)):
        return None
    if not url.startswith(('https://', 'http://')):
        return None

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


class MoonycOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='moonyc_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = make_session()
        records = []
        page = 1
        try:
            while True:
                params = {
                    'page': page,
                    'per_page': 50,
                    'start_date': '1900-01-01 00:00:00',
                    'end_date': '2100-12-31 23:59:59',
                    'status': 'publish',
                }
                response = session.get(API_URL, params=params, timeout=45)
                response.raise_for_status()
                payload = response.json()

                events = payload.get('events') or []
                for event in events:
                    record = parse_event(event)
                    if record:
                        records.append(record)
                    else:
                        log_message(
                            'Skipping Modus Operandi Orchestra event with incomplete details',
                            event='crawler_event_skipped',
                            level='warning',
                            url=clean_text(event.get('url')) or API_URL,
                            event_id=event.get('id'),
                        )

                total_pages = int(payload.get('total_pages') or 1)
                if page >= total_pages:
                    break
                page += 1
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Modus Operandi Orchestra events API',
                event='crawler_api_request_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        finally:
            session.close()

        if not records:
            log_message(
                'No Modus Operandi Orchestra events found',
                event='crawler_empty_listing',
                level='warning',
                url=API_URL,
                record_count=0,
            )
        # The API occasionally contains a superseded duplicate occurrence with
        # an outdated venue relationship. Prefer the later API item for the
        # same advertised performance.
        unique_records = {
            (item['title'], item['date'], item['time_from']): item
            for item in records
        }
        return sorted(
            unique_records.values(),
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
        )


def main():
    MoonycOrgCrawler().run()


if __name__ == '__main__':
    main()
