from datetime import datetime
import re

from bs4 import BeautifulSoup
from curl_cffi import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.edfringe.com/'
SOURCE = 'Edinburgh Festival Fringe'
API_URL = 'https://edfringe-tikketr-web-api.equhost.com'
TOKEN_URL = f'{API_URL}/token'
GRAPHQL_URL = f'{API_URL}/graphql'
EVENT_URL = f'{SOURCE_URL}tickets/whats-on/'
COUNTRY_CODE = 'GB'
CITY = 'Edinburgh'

# The public web client uses this anonymous account to obtain its short-lived
# read-only catalogue token. The values are shipped in the site's JavaScript.
ANONYMOUS_LOGIN = {
    'username': 'anonymous',
    'password': '2add50c2-ac54-4c1e-b5bc-f8d9ca66a067',
}

# Music and Musicals and Opera contain most eligible performances. Ballet is a
# cross-genre subgenre and also finds relevant events in the dance category.
# The feeds intentionally remain candidates: every one contains events outside
# the project's scope and narrower tags demonstrably omit eligible crossover.
CANDIDATE_FILTERS = (
    {'genres': ['MUSIC']},
    {'genres': ['OPERA']},
    {'subgenres': ['BALLET']},
)

PAGE_SIZE = 100

EVENTS_QUERY = '''
query EventsSearch($criteria: SearchCriteriaInput!) {
  events(input: $criteria) {
    total
    page
    per
    results {
      id
      title
      slug
      description
      venues { title slug }
      performances {
        id
        dateTime
        cancelled
      }
    }
  }
}
'''


def clean_text(value):
    if value is None:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def api_session():
    session = requests.Session(impersonate='chrome')
    session.headers.update({
        'Accept': 'application/json',
        'Referer': SOURCE_URL,
    })
    response = session.post(TOKEN_URL, json=ANONYMOUS_LOGIN, timeout=45)
    response.raise_for_status()
    token = response.json().get('token')
    if not token:
        raise ValueError('The EdFringe API did not return an anonymous token')
    session.headers['Authorization'] = f'Bearer {token}'
    return session


def api_events(session, event_filter):
    page = 0
    while True:
        criteria = {
            **event_filter,
            'isFuture': False,
            'page': page,
            'per': PAGE_SIZE,
            'sortBy': 'DATE',
        }
        response = session.post(
            GRAPHQL_URL,
            json={
                'operationName': 'EventsSearch',
                'query': EVENTS_QUERY,
                'variables': {'criteria': criteria},
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get('errors'):
            raise ValueError('The EdFringe events query returned GraphQL errors')
        result = (payload.get('data') or {}).get('events') or {}
        events = result.get('results') or []
        yield from events

        total = int(result.get('total') or 0)
        if not events or (page + 1) * PAGE_SIZE >= total:
            break
        page += 1


def parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def event_records(event):
    title = clean_text(event.get('title'))
    slug = clean_text(event.get('slug'))
    description = clean_text(event.get('description')) or None
    venues = {
        clean_text(venue.get('title'))
        for venue in event.get('venues') or []
        if clean_text(venue.get('title'))
    }

    # The API does not associate individual performances with a venue when a
    # production has several venues, so such occurrences cannot be paired
    # defensibly and are skipped.
    if not title or not slug or len(venues) != 1:
        return []
    venue = next(iter(venues))
    url = f'{EVENT_URL}{slug}'

    records = []
    for performance in event.get('performances') or []:
        starts_at = parse_datetime(performance.get('dateTime'))
        if starts_at is None:
            continue
        records.append({
            'title': title,
            'date': starts_at.date().isoformat(),
            'url': url,
            'time_from': starts_at.strftime('%H:%M'),
            'venue': venue,
            'city': CITY,
            'country_code': COUNTRY_CODE,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class EdfringeComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='edfringe_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = api_session()
        events = {}
        for event_filter in CANDIDATE_FILTERS:
            try:
                for event in api_events(session, event_filter):
                    events[event.get('id')] = event
            except (requests.RequestsError, ValueError, KeyError) as error:
                log_message(
                    'Failed to scrape an EdFringe candidate feed',
                    event='crawler_feed_failed',
                    level='warning',
                    url=GRAPHQL_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

        records = []
        for event in events.values():
            records.extend(event_records(event))
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'],
                record['venue'],
            ),
        )


def main():
    EdfringeComCrawler().run()


if __name__ == '__main__':
    main()
