import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://altoonasymphonyorchestra.com/'
SOURCE = 'Altoona Symphony Orchestra'
TICKETS_URL = 'https://mishlertheatre.vbotickets.com/events'
PLUGIN_ROOT = 'https://plugin.vbotickets.com'
LOAD_PLUGIN_URL = f'{PLUGIN_ROOT}/plugin/loadplugin'
EVENTS_URL = f'{PLUGIN_ROOT}/Plugin/events/showevents'
SITE_ID = 'D4263988-F162-4FE6-8E8B-BB12B368F750'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_occurrence(value):
    text = clean_text(value)
    match = re.search(
        r'(?:[A-Za-z]{3},\s*)?(\d{1,2}/\d{1,2}/\d{4})\s*@\s*'
        r'(\d{1,2}:\d{2}\s*[AP]M)',
        text,
        re.I,
    )
    if not match:
        return None, None
    try:
        date = datetime.strptime(match.group(1), '%m/%d/%Y').date().isoformat()
        time_from = datetime.strptime(match.group(2).upper(), '%I:%M %p').strftime('%H:%M')
    except ValueError:
        return None, None
    return date, time_from


def parse_event(item):
    title = clean_text(item.get('data-event-name'))
    if (
        clean_text(item.get('data-event-category')).casefold() != 'aso performance'
        or clean_text(item.get('data-event-subcategory')).casefold() != 'symphony'
    ):
        return None

    date, time_from = parse_occurrence(item.select_one('.TextEventDate'))
    venue = clean_text(item.select_one('.TextVenueName'))
    address = clean_text(item.select_one('.TextVenueAddress'))
    city_match = re.search(r',\s*([^,]+),\s*PA\s+\d{5}\b', address, re.I)
    city = clean_text(city_match.group(1)) if city_match else ''
    link = item.select_one('.EventListPoster a[href], .HeaderEventName a[href]')
    event_id_match = re.search(r'\bEID(\d+)\b', ' '.join(item.get('class', [])))
    event_url = urljoin(PLUGIN_ROOT, link.get('href', '')) if link else ''
    if not event_url and event_id_match:
        event_url = f'{TICKETS_URL}?eid={event_id_match.group(1)}'

    if not all((title, date, event_url, venue, city)):
        return None

    description = clean_text(item.select_one('.EventIntroText')) or None
    return {
        'title': title,
        'date': date,
        'url': event_url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class AltoonaSymphonyOrchestraComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='altoonasymphonyorchestra_com',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(
            LOAD_PLUGIN_URL,
            params={
                'siteid': SITE_ID,
                'page': 'ListEvents',
                'w': 1280,
                'h': 720,
                'o': 8250,
                'eid': 0,
                'edid': 0,
                'did': 0,
                'wlid': 0,
                'parent': 'mishlertheatre.vbotickets.com',
                'forceevent': 0,
                'parenturl': TICKETS_URL,
                'PluginType': 'Embed',
            },
            timeout=45,
        )
        response.raise_for_status()
        session_match = re.search(r'/plugin/events\?s=([0-9a-f-]+)', response.text, re.I)
        if not session_match:
            raise RuntimeError('VBO ticketing session identifier was not found')

        session_id = session_match.group(1)
        records = []
        for event_type in ('current', 'past'):
            response = session.get(
                EVENTS_URL,
                params={
                    'ViewType': 'list',
                    'EventType': event_type,
                    'day': '',
                    's': session_id,
                },
                timeout=45,
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            for item in soup.select('.EventListWrapper'):
                if (
                    clean_text(item.get('data-event-category')).casefold() != 'aso performance'
                    or clean_text(item.get('data-event-subcategory')).casefold() != 'symphony'
                ):
                    continue
                record = parse_event(item)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Altoona Symphony Orchestra event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=TICKETS_URL,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, URL, venue, or city is missing',
                    )

        unique = {
            (item['title'], item['date'], item['time_from'], item['venue'], item['city']): item
            for item in records
        }
        return sorted(
            unique.values(),
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    AltoonaSymphonyOrchestraComCrawler().run()


if __name__ == '__main__':
    main()
