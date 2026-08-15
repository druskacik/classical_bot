import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.opera.si/'
CALENDAR_URL = urljoin(SOURCE_URL, 'sl/program/koledar')
GRAPHQL_URL = urljoin(SOURCE_URL, 'graphql')
SOURCE = 'SNG Opera in balet Ljubljana'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'sl-SI,sl;q=0.9,en;q=0.7',
}

EVENT_QUERY = '''
query ReadEventShows($startDate: String!, $endDate: String!, $limit: Int, $offset: Int) {
  readEventShows(
    filter: {startDate: {gte: $startDate, lte: $endDate}}
    limit: $limit
    offset: $offset
    sort: {startDate: ASC}
  ) {
    nodes {
      id
      startDate
      startTime
      location
      event {
        id
        title
        eventAuthor
        eventType
        eventTypeName
        subtitle
        link
        urlSegment
      }
    }
  }
}
'''

# The calendar location field names venues rather than providing structured
# addresses. These are all locations currently retained by its published API.
LOCATION_MAP = {
    'sng opera in balet ljubljana': ('SNG Opera in balet Ljubljana', 'Ljubljana', 'SI'),
    'spodnji foyer sng opera in balet ljubljana': (
        'Spodnji foyer SNG Opera in balet Ljubljana', 'Ljubljana', 'SI'
    ),
    'cankarjev dom': ('Cankarjev dom', 'Ljubljana', 'SI'),
    'narodna galerija': ('Narodna galerija', 'Ljubljana', 'SI'),
    'teatro lirico giuseppe verdi trieste': (
        'Teatro Lirico Giuseppe Verdi', 'Trieste', 'IT'
    ),
    'mestno gledališče celovec i stadttheater klagenfurt': (
        'Stadttheater Klagenfurt', 'Klagenfurt am Wörthersee', 'AT'
    ),
    'avditorij portorož': ('Avditorij Portorož', 'Portorož', 'SI'),
    'hnk zagreb': ('Hrvatsko narodno kazalište u Zagrebu', 'Zagreb', 'HR'),
    'les brigittines, brussels': ('Les Brigittines', 'Brussels', 'BE'),
    'akademija za glasbo, ul': ('Akademija za glasbo UL', 'Ljubljana', 'SI'),
    'oder -3 (dvorana cirila debevca)': (
        'Dvorana Cirila Debevca, SNG Opera in balet Ljubljana', 'Ljubljana', 'SI'
    ),
    'kulturni dom franca bernika domžale': (
        'Kulturni dom Franca Bernika Domžale', 'Domžale', 'SI'
    ),
    'orfejev salon': ('Orfejev salon, SNG Opera in balet Ljubljana', 'Ljubljana', 'SI'),
    'dvorana lucijana marije škerjanca kgbl': (
        'Dvorana Lucijana Marije Škerjanca KGBL', 'Ljubljana', 'SI'
    ),
    'arena pula': ('Arena Pula', 'Pula', 'HR'),
    'sng opera in balet ljubljana, oder -3': (
        'Oder -3, SNG Opera in balet Ljubljana', 'Ljubljana', 'SI'
    ),
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_page_url(show):
    event = show.get('event') or {}
    link = clean_text(event.get('link'))
    show_id = clean_text(show.get('id'))
    if not link or not show_id:
        return ''
    parts = urlsplit(urljoin(SOURCE_URL, link))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, f'd={show_id}', ''))


def parse_time(value):
    match = re.fullmatch(r'([01]\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?', clean_text(value))
    return f'{match.group(1)}:{match.group(2)}' if match else None


def make_record(show, description=None):
    event = show.get('event') or {}
    title = clean_text(event.get('title'))
    event_date = clean_text(show.get('startDate'))
    url = event_page_url(show)
    location = LOCATION_MAP.get(clean_text(show.get('location')).casefold())
    try:
        event_date = date.fromisoformat(event_date).isoformat()
    except ValueError:
        return None
    if not title or not url or not location:
        return None

    venue, city, country_code = location
    summary = [clean_text(event.get(key)) for key in ('eventAuthor', 'subtitle')]
    summary = [value for value in summary if value]
    body = clean_text(description)
    if body:
        summary.append(body)
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(show.get('startTime')),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(dict.fromkeys(summary)) or None,
    }


class OperaSiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_si',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='SI',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date'],
    )

    def _get_shows(self, session):
        shows = []
        offset = 0
        while True:
            response = session.post(
                GRAPHQL_URL,
                json={
                    'operationName': 'ReadEventShows',
                    'variables': {
                        'startDate': '2000-01-01',
                        'endDate': '2100-12-31',
                        'limit': 100,
                        'offset': offset,
                    },
                    'query': EVENT_QUERY,
                },
                timeout=60,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get('errors'):
                raise ValueError(f'Opera.si GraphQL error: {payload["errors"][0].get("message")}')
            page = (((payload.get('data') or {}).get('readEventShows') or {}).get('nodes') or [])
            shows.extend(page)
            if len(page) < 100:
                return shows
            offset += len(page)

    def _description(self, session, show):
        url = event_page_url(show)
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            return clean_text(soup.select_one('.event-description')) or None
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Opera.si event detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return None

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            shows = self._get_shows(session)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Opera.si calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        descriptions = {}
        unique_events = {}
        for show in shows:
            event = show.get('event') or {}
            if event.get('title') and event.get('id') and event_page_url(show):
                unique_events.setdefault(str(event['id']), show)

        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {
                executor.submit(self._description, session, show): event_id
                for event_id, show in unique_events.items()
            }
            for future in as_completed(futures):
                descriptions[futures[future]] = future.result()

        records = []
        for show in shows:
            event_id = str((show.get('event') or {}).get('id') or '')
            record = make_record(show, descriptions.get(event_id))
            if record:
                records.append(record)
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
        )


def main():
    OperaSiCrawler().run()


if __name__ == '__main__':
    main()
