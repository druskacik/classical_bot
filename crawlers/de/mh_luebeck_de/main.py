import html
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mh-luebeck.de/'
API_URL = 'https://mhl-cs.e-fork.net/graphql'
SOURCE = 'Musikhochschule Lübeck'
HEADERS = {
    'User-Agent': 'classical-concert-crawler/1.0',
    'Accept-Language': 'de-DE,de;q=0.9',
}
PAGE_SIZE = 250
QUERY = '''
query Events($offset: Int!, $limit: Int!, $date: String!) {
  entityQuery(
    entityType: NODE
    filter: {
      conditions: [
        {field: "type", value: ["event"], operator: IN}
        {field: "status", value: ["1"], operator: IN}
      ]
      groups: [{conjunction: OR, conditions: [
        {field: "field_date", value: [$date], operator: GREATER_THAN}
      ]}]
    }
    offset: $offset
    limit: $limit
    sort: [{field: "field_date", direction: ASC}]
  ) {
    total
    items {
      ... on NodeEvent {
        id
        title
        url { path }
        fieldDate { value endValue }
        fieldLocation
        fieldLocationAddition
        fieldText
      }
    }
  }
}
'''

# The calendar is based in Lübeck, but it also advertises a small number of
# explicitly touring performances.  These markers prevent the home-city
# default from being applied to those events.
CITY_MARKERS = {
    'bad oldesloe': 'Bad Oldesloe',
    'blumendorf': 'Bad Oldesloe',
    'schloss plön': 'Plön',
    'muthesius': 'Kiel',
    'theater kiel': 'Kiel',
    'petruskirche kiel': 'Kiel',
    'kulturhof itzehoe': 'Itzehoe',
    'timmendorfer': 'Timmendorfer Strand',
    'augustinum mölln': 'Mölln',
    'kurhaus malente': 'Bad Malente-Gremsmühlen',
    'kursaal malente': 'Bad Malente-Gremsmühlen',
    'bad bramstedt': 'Bad Bramstedt',
    'meldorfer dom': 'Meldorf',
    'leck,': 'Leck',
    'ammersbek': 'Ammersbek',
    'hasselburg': 'Altenkrempe',
    'hamburg': 'Hamburg',
}


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('POST',),
    )))
    return session


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    # A few migrated records contain UTF-8 decoded as Latin-1.
    if 'Ã' in text or 'Â' in text:
        try:
            text = text.encode('latin-1').decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    text = text.replace('_x000d__x000a_', '\n')
    text = text.replace('\xa0', ' ').replace('\u00ad', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def city_from_venue(venue):
    folded = venue.casefold()
    for marker, city in CITY_MARKERS.items():
        if marker in folded:
            return city
    return 'Lübeck'


def parse_item(item):
    title = clean_text(item.get('title'))
    location = clean_text(item.get('fieldLocation'))
    addition = clean_text(item.get('fieldLocationAddition'))
    description = clean_text(item.get('fieldText'))
    venue = addition or location
    inferred_location = ''
    if not venue and 'Kabäuschen des Heiligen-Geist-Hospitals' in description:
        venue = 'Heiligen-Geist-Hospital'
    if not venue:
        stated_venue = re.search(r'\bOrt:\s*([^\n]+)', description, re.IGNORECASE)
        if stated_venue:
            inferred_location = stated_venue.group(1).replace('_x000d__x000a_', ' ')
            venue = inferred_location.split('/')[0].strip()
    path = (item.get('url') or {}).get('path')
    raw_date = (item.get('fieldDate') or {}).get('value')
    if not title or not venue or not path or not raw_date:
        return None
    try:
        moment = datetime.fromisoformat(raw_date)
        event_date = moment.date().isoformat()
    except (TypeError, ValueError):
        return None
    venue_context = ' '.join(
        part for part in (location, addition, inferred_location, venue) if part
    )
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, path),
        'time_from': moment.strftime('%H:%M'),
        'venue': venue,
        'city': city_from_venue(venue_context),
        'country_code': 'DE',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_page(session, offset):
    response = session.post(
        API_URL,
        json={
            'operationName': 'Events',
            'variables': {
                'offset': offset,
                'limit': PAGE_SIZE,
                # This predates the site's available archive and therefore
                # includes every published past and future event.
                'date': '2000-01-01T00:00:00.000Z',
            },
            'query': QUERY,
        },
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get('errors'):
        raise ValueError(f"GraphQL returned {len(payload['errors'])} error(s)")
    return payload['data']['entityQuery']


def get_concerts():
    session = make_session()
    offset = 0
    items = []
    total = None
    while total is None or offset < total:
        try:
            page = fetch_page(session, offset)
        except (requests.RequestException, ValueError, KeyError) as error:
            log_message(
                'Failed to scrape Musikhochschule Lübeck event API',
                event='crawler_page_failed', level='warning', url=API_URL,
                offset=offset, error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        total = int(page['total'])
        page_items = page.get('items') or []
        items.extend(page_items)
        if not page_items:
            break
        offset += len(page_items)

    records = [record for item in items if (record := parse_item(item))]
    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(unique.values(), key=lambda record: (
        record['date'], record['time_from'] or '', record['city'],
        record['title'], record['url'],
    ))


class MhLuebeckDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mh_luebeck_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    MhLuebeckDeCrawler().run()


if __name__ == '__main__':
    main()
