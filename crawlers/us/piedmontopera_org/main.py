import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.piedmontopera.org/'
SOURCE = 'Piedmont Opera'
CALENDAR_URL = f'{SOURCE_URL}calendar'
CALENDAR_API_URL = 'https://calendar.apiboomtech.com/api/published_calendar'
CALENDAR_APP_ID = '13b4a028-00fa-7133-242f-4628106b8c91'
CALENDAR_COMPONENT_ID = 'comp-krf3mifv'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

STATE_CITY_RE = re.compile(r',\s*([^,]+?),\s*[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?(?:,|$)')
ADDRESS_VENUES = {
    '101 W 5th St, Winston-Salem, NC 27101, USA': 'The Millennium Event Center',
    '129 W Main St, Elkin, NC 28621, USA': 'The Reeves Theatre',
    '200 Brookstown Ave, Winston-Salem, NC 27101, USA': 'The Historic Brookstown Inn',
    '220 E Commerce Ave, High Point, NC 27260, USA': 'High Point Theatre',
    '450 Groce Rd, Ronda, NC 28670, USA': 'Raffaldini Vineyards',
    '600 Holly Ave NW, Winston-Salem, NC 27101, USA': 'Calvary Moravian Church',
    '646 W 5th St, Winston-Salem, NC 27101, USA': 'Centenary United Methodist Church',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_start(value):
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None, None
    event_time = parsed.strftime('%H:%M') if 'T' in str(value) else None
    return parsed.date().isoformat(), event_time


def city_from_address(address):
    matches = STATE_CITY_RE.findall(clean_text(address))
    return clean_text(matches[-1]) if matches else ''


def venue_from_data(venue_data):
    name = clean_text(venue_data.get('name'))
    address = clean_text(venue_data.get('address'))
    if name:
        return name
    if address in ADDRESS_VENUES:
        return ADDRESS_VENUES[address]
    first_part = address.split(',', 1)[0].strip()
    if first_part and not re.match(r'^\d', first_part):
        return first_part.split('|', 1)[0].strip()
    return ''


def fetch_calendar(session):
    page = session.get(CALENDAR_URL, timeout=45)
    page.raise_for_status()

    token_response = session.get(f'{SOURCE_URL}_api/v1/access-tokens', timeout=45)
    token_response.raise_for_status()
    app = token_response.json().get('apps', {}).get(CALENDAR_APP_ID, {})
    instance = app.get('instance') or app.get('dataToken')
    if not instance:
        raise ValueError('Boom Calendar access token is missing')

    response = session.get(
        CALENDAR_API_URL,
        params={
            'comp_id': CALENDAR_COMPONENT_ID,
            'instance': instance,
            'originCompId': '',
            'time_zone': 'America/New_York',
        },
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload.get('events'), list):
        raise ValueError('Boom Calendar response has no events list')
    return payload['events']


def event_record(event):
    title = clean_text(event.get('title'))
    event_date, time_from = parse_start(event.get('start'))
    venue_data = event.get('venue') if isinstance(event.get('venue'), dict) else {}
    venue = venue_from_data(venue_data)
    city = city_from_address(venue_data.get('address'))
    event_id = event.get('id')

    named_city = next(
        (candidate for candidate in ('High Point', 'Kernersville', 'Winston-Salem') if candidate in venue),
        None,
    )

    # A street address is not a venue name. Records without a named venue or a
    # parseable city, or with contradictory location fields, are omitted rather
    # than manufacturing invalid location data.
    if not all((title, event_date, event_id, venue, city)) or (named_city and named_city != city):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': f'{CALENDAR_URL}#event-{event_id}',
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('desc')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    events = fetch_calendar(session)
    records = [record for event in events if (record := event_record(event))]

    if not records:
        log_message(
            'No valid calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class PiedmontOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='piedmontopera_org',
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
    PiedmontOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
