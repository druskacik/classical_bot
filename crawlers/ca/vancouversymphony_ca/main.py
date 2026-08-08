import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.vancouversymphony.ca/'
CALENDAR_URL = f'{SOURCE_URL}concerts/full-calendar/'
GRAPHQL_URL = f'{SOURCE_URL}graphql'
SOURCE = 'Vancouver Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-CA,en;q=0.9',
}

# The API exposes venue names but not addresses. These are the cities of the
# named venues in the VSO calendar; an unrecognised venue is skipped so touring
# performances are never assigned the orchestra's home city by accident.
VENUE_CITIES = {
    'Orpheum': 'Vancouver',
    'Bell Performing Arts Centre': 'Surrey',
    'Pyatt Hall': 'Vancouver',
    'Centennial Theatre': 'North Vancouver',
    'Chan Centre at UBC': 'Vancouver',
    'Christ Church Cathedral': 'Vancouver',
    'Deer Lake Park': 'Burnaby',
    'David Lam Park': 'Vancouver',
    'South Delta Baptist Church': 'Delta',
    'Vancouver Playhouse': 'Vancouver',
    'Annex': 'Vancouver',
}

EVENTS_QUERY = '''
query Events($after: String) {
  events(first: 100, after: $after, where: {orderby: {field: MODIFIED, order: DESC}}) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        title
        slug
        eventCustomFields {
          description
          dates {
            date
            time
            venue { ... on Venue { title } }
          }
        }
      }
    }
  }
}
'''


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_events(session):
    events = []
    cursor = None
    while True:
        response = session.post(
            GRAPHQL_URL,
            json={'query': EVENTS_QUERY, 'variables': {'after': cursor}},
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get('errors'):
            raise ValueError(f'GraphQL returned {len(payload["errors"])} error(s)')
        connection = payload['data']['events']
        events.extend(edge['node'] for edge in connection.get('edges') or [])
        page_info = connection['pageInfo']
        if not page_info['hasNextPage']:
            return events
        cursor = page_info['endCursor']


def event_url(event):
    slug = event.get('slug')
    return f'{SOURCE_URL}event/{slug}/' if slug else ''


def detail_description(session, event):
    url = event_url(event)
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    # This panel contains the complete programme followed by the event's long
    # description, preserving composer/work names that the listing API omits.
    panel = soup.select_one('#event-program-panel')
    return clean_text(panel) or clean_text(
        (event.get('eventCustomFields') or {}).get('description')
    ) or None


def parse_date(value):
    if not value or not re.fullmatch(r'\d{8}', str(value)):
        return None
    try:
        return date.fromisoformat(f'{value[:4]}-{value[4:6]}-{value[6:]}').isoformat()
    except ValueError:
        return None


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip().lower(), '%I:%M %p').strftime('%H:%M')
    except ValueError:
        return None


def make_records(event, description):
    title = clean_text(event.get('title'))
    url = event_url(event)
    custom_fields = event.get('eventCustomFields') or {}
    if not title or not url:
        return []

    records = []
    for performance in custom_fields.get('dates') or []:
        venue = clean_text((performance.get('venue') or {}).get('title'))
        city = VENUE_CITIES.get(venue)
        event_date = parse_date(performance.get('date'))
        if not event_date or not venue or not city:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(performance.get('time')),
            'venue': venue,
            'city': city,
            'country_code': 'CA',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = fetch_events(session)
    descriptions = {}

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(detail_description, session, event): event for event in events}
        for future in as_completed(futures):
            event = futures[future]
            try:
                descriptions[event.get('slug')] = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=event_url(event),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                descriptions[event.get('slug')] = clean_text(
                    (event.get('eventCustomFields') or {}).get('description')
                ) or None

    records = [
        record
        for event in events
        for record in make_records(event, descriptions.get(event.get('slug')))
    ]
    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title'], record['venue']),
    )


class VancouverSymphonyCaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='vancouversymphony_ca',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CA',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    VancouverSymphonyCaCrawler().run()


if __name__ == '__main__':
    main()
