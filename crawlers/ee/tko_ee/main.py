import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.tko.ee/'
EVENTS_URL = f'{SOURCE_URL}kontserdid'
SOURCE = 'Tallinna Kammerorkester'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'et-EE,et;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not isinstance(value, str):
        return ''
    value = value.replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def warmup_data(html):
    script = BeautifulSoup(html, 'html.parser').find('script', id='wix-warmup-data')
    if script is None or not script.string:
        raise ValueError('Wix warmup data was not found')
    return json.loads(script.string)


def find_event_collections(data):
    """Return event summaries from all Wix Events widgets on the page."""
    collections = []

    def visit(value):
        if isinstance(value, dict):
            events = value.get('events')
            if (
                isinstance(events, dict)
                and isinstance(events.get('events'), list)
            ):
                collections.extend(events['events'])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(data.get('appsWarmupData', {}))
    return collections


def find_detail_event(data):
    def visit(value):
        if isinstance(value, dict):
            if all(key in value for key in ('title', 'location', 'scheduling', 'slug')):
                return value
            for child in value.values():
                result = visit(child)
                if result:
                    return result
        elif isinstance(value, list):
            for child in value:
                result = visit(child)
                if result:
                    return result
        return None

    return visit(data.get('appsWarmupData', {}))


def rich_text(value):
    if not isinstance(value, dict):
        return ''
    blocks = []
    for node in value.get('nodes', []):
        parts = []

        def collect(item):
            if not isinstance(item, dict):
                return
            text = item.get('textData', {}).get('text')
            if isinstance(text, str):
                parts.append(text)
            for child in item.get('nodes', []):
                collect(child)

        collect(node)
        line = clean_text(''.join(parts))
        if line:
            blocks.append(line)
    return clean_text('\n'.join(blocks))


def parse_event(event, url):
    location = event.get('location') or {}
    full_address = location.get('fullAddress') or {}
    venue = clean_text(location.get('name'))
    city = clean_text(full_address.get('city'))
    country_code = clean_text(full_address.get('country')).upper()
    scheduling = event.get('scheduling') or {}
    config = scheduling.get('config') or {}
    start = config.get('startDate')
    time_from = clean_text(scheduling.get('startTimeFormatted')) or None

    try:
        start_datetime = datetime.fromisoformat(start.replace('Z', '+00:00'))
        timezone = ZoneInfo(config.get('timeZoneId') or 'Europe/Tallinn')
        event_date = start_datetime.astimezone(timezone).date().isoformat()
    except (AttributeError, KeyError, ValueError):
        return None

    title = clean_text(event.get('title'))
    if not all((title, venue, city, country_code, url)):
        return None

    long_description = rich_text(event.get('longDescription'))
    summary = clean_text(event.get('description'))
    description = clean_text('\n\n'.join(filter(None, (summary, long_description)))) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class TkoEeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tko_ee',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='EE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def _fetch_detail(self, summary):
        slug = clean_text(summary.get('slug'))
        if not slug:
            return None
        url = f'{SOURCE_URL}event-details/{slug}'
        try:
            response = requests.get(url, headers=HEADERS, timeout=45)
            response.raise_for_status()
            event = find_detail_event(warmup_data(response.text)) or summary
            return parse_event(event, url)
        except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
            log_message(
                'Failed to fetch TKO event detail',
                event='crawler_fetch_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return parse_event(summary, url)

    def scrape(self):
        try:
            response = requests.get(EVENTS_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
            summaries = find_event_collections(warmup_data(response.text))
        except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
            log_message(
                'Failed to fetch TKO concert calendar',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        unique = {
            event.get('id') or event.get('slug'): event
            for event in summaries
            if event.get('id') or event.get('slug')
        }
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(self._fetch_detail, event) for event in unique.values()]
            for future in as_completed(futures):
                record = future.result()
                if record:
                    records.append(record)

        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['url']))


def main():
    TkoEeCrawler().run()


if __name__ == '__main__':
    main()
