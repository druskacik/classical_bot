import re
from datetime import datetime

import requests
import urllib3
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://conservatoire.nantes.fr/'
EVENTS_URL = f'{SOURCE_URL}agenda/evenements/'
EVENTS_API = f'{SOURCE_URL}wp-admin/admin-ajax.php'
SOURCE = 'Conservatoire de Nantes'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def french_value(value):
    if isinstance(value, dict):
        return value.get('fr') or value.get('en') or next(iter(value.values()), '')
    return value or ''


def listing_events(session):
    events = []
    page = 1
    while True:
        response = session.get(
            EVENTS_API,
            params={
                'postId': 211,
                'action': 'update_events',
                'view': 'list',
                'page': page,
                'size': 100,
                # OpenAgenda otherwise returns only current and future events.
                # Adding "passed" retains those and includes the available archive.
                'relative[0]': 'passed',
            },
            timeout=60,
            verify=False,
        )
        response.raise_for_status()
        payload = response.json()
        page_events = payload.get('events') or []
        events.extend(page_events)
        total = int(payload.get('total') or 0)
        if not page_events or len(events) >= total:
            return events
        page += 1


def event_description(event):
    parts = []
    for value in (event.get('longDescription'), event.get('description')):
        text = clean_text(french_value(value))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def event_records(event):
    title = clean_text(french_value(event.get('title')))
    slug = clean_text(event.get('slug'))
    location = event.get('location') or {}
    venue = clean_text(location.get('name'))
    city = clean_text(location.get('city'))
    country_code = clean_text(location.get('countryCode')).upper()
    if not all((title, slug, venue, city, country_code)) or len(country_code) != 2:
        return []
    # Some OpenAgenda records use a municipality or neighbourhood as their
    # location. That is not a defensible performance venue.
    venue_key = re.sub(r'\s*\([^)]*\)\s*$', '', venue).casefold()
    if venue_key == city.casefold():
        return []

    url = f'{EVENTS_URL}{slug}'
    description = event_description(event)
    records = []
    for timing in event.get('timings') or []:
        begin = timing.get('begin')
        try:
            parsed = datetime.fromisoformat(begin)
        except (TypeError, ValueError):
            continue
        records.append({
            'title': title,
            'date': parsed.date().isoformat(),
            'url': url,
            'time_from': (
                None if event.get('nm-ignorer-heure-debut')
                else parsed.strftime('%H:%M')
            ),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    # The server currently omits an intermediate certificate. Browser clients
    # can load it, but Requests cannot validate that incomplete chain.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        events = listing_events(session)
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Failed to fetch Conservatoire de Nantes events',
            event='crawler_fetch_failed',
            level='error',
            url=EVENTS_API,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    records = [record for event in events for record in event_records(event)]
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class ConservatoireNantesFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='conservatoire_nantes_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    ConservatoireNantesFrCrawler().run()


if __name__ == '__main__':
    main()
