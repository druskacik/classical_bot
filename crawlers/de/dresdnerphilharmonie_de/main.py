from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.dresdnerphilharmonie.de/de/'
SOURCE = 'Dresdner Philharmonie'
API_URL = 'https://www.dresdnerphilharmonie.de/api/event/'
TIMEZONE = ZoneInfo('Europe/Berlin')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

# The API supplies only the venue name. These are all venues present in the
# available current-season and archive feed; the non-Dresden entries are tour
# performances and must not inherit the orchestra's home city.
VENUE_CITIES = {
    'Konzertsaal im Kulturpalast, Dresden': 'Dresden',
    'Kreuzkirche': 'Dresden',
    'Kulturpalast, Konzertsaal': 'Dresden',
    'St. Johannis-Kirche': 'Bad Schandau',
    'Theaterplatz vor der Semperoper': 'Dresden',
    'Villa Kolbe': 'Radebeul',
}


def clean_text(value):
    if value is None:
        return ''
    return ' '.join(str(value).replace('\xa0', ' ').split())


def fetch_events(session):
    events = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={
                'language': 'de',
                'limit': 60,
                'page': page,
                'payload[only_future]': 'false',
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        events.extend(payload.get('teaser') or [])
        if payload.get('lastPage', True):
            return events
        page += 1


def event_description(extension):
    parts = []
    for key in ('description', 'finder_desc'):
        text = clean_text(extension.get(key))
        if text and text not in parts:
            parts.append(text)

    programme = []
    for work in extension.get('program') or []:
        composer = clean_text(work.get('composer'))
        title = clean_text(work.get('title'))
        if title:
            programme.append(f'{composer}: {title}' if composer else title)
    if programme:
        parts.append('Programm\n' + '\n'.join(programme))

    contributors = []
    for contributor in extension.get('contributors') or []:
        name = clean_text(contributor.get('name'))
        role = clean_text(contributor.get('role'))
        if name:
            contributors.append(f'{name} – {role}' if role else name)
    if contributors:
        parts.append('Mitwirkende\n' + '\n'.join(contributors))
    return '\n\n'.join(parts) or None


def make_record(event):
    teaser = event.get('teaser') or {}
    extension = event.get('extension') or {}

    # The calendar is a classical-performance calendar, but it also exposes
    # organ tours under the explicit first-party genre "Führung".
    genres = [clean_text(value) for value in (extension.get('filter') or {}).get('genre') or []]
    if 'Führung' in genres:
        return None

    title = clean_text(teaser.get('title'))
    venue = clean_text(extension.get('venue'))
    city = VENUE_CITIES.get(venue)
    link = (teaser.get('link') or {}).get('url')
    timestamp = extension.get('date')
    if not title or not venue or not city or not link or not isinstance(timestamp, (int, float)):
        return None

    try:
        start = datetime.fromtimestamp(timestamp, TIMEZONE)
    except (OverflowError, OSError, ValueError):
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, link),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': event_description(extension),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class DresdnerphilharmonieDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='dresdnerphilharmonie_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            events = fetch_events(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Dresdner Philharmonie event feed',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = [record for event in events if (record := make_record(event))]
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    DresdnerphilharmonieDeCrawler().run()


if __name__ == '__main__':
    main()
