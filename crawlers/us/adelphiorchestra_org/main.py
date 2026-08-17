import html
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.adelphiorchestra.org/'
SOURCE = 'Adelphi Orchestra'
EVENT_URL_PREFIX = f'{SOURCE_URL}event-details-registration/'
HEADERS = {
    'Accept': 'text/html,application/xhtml+xml',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ')
    return re.sub(r'[ \t]+', ' ', text).strip()


def find_events(warmup_data):
    for app_data in warmup_data.get('appsWarmupData', {}).values():
        if not isinstance(app_data, dict):
            continue
        for widget_data in app_data.values():
            if not isinstance(widget_data, dict):
                continue
            events_data = widget_data.get('events')
            if isinstance(events_data, dict) and isinstance(events_data.get('events'), list):
                return events_data['events']
    return []


def local_start(event):
    scheduling = event.get('scheduling') or {}
    config = scheduling.get('config') or {}
    value = config.get('startDate')
    timezone = config.get('timeZoneId')
    if not value or not timezone:
        return None
    try:
        start = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return start.astimezone(ZoneInfo(timezone))
    except (TypeError, ValueError, KeyError):
        return None


def records_from_events(events):
    records = []
    for event in events:
        title = clean_text(event.get('title'))
        slug = clean_text(event.get('slug'))
        location = event.get('location') or {}
        venue = clean_text(location.get('name'))
        full_address = location.get('fullAddress') or {}
        city = clean_text(full_address.get('city'))
        country_code = clean_text(full_address.get('country')).upper()
        starts_at = local_start(event)
        if not all((title, slug, venue, city, country_code, starts_at)):
            continue

        description_parts = []
        for field in ('description', 'about'):
            value = clean_text(event.get(field))
            if value and value not in description_parts:
                description_parts.append(value)

        records.append({
            'title': title,
            'date': starts_at.date().isoformat(),
            'url': f'{EVENT_URL_PREFIX}{slug}',
            'time_from': starts_at.strftime('%H:%M'),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': '\n\n'.join(description_parts) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return sorted(records, key=lambda row: (row['date'], row['time_from'], row['title']))


def scrape_concerts(session=None):
    session = session or requests.Session()
    response = session.get(SOURCE_URL, headers=HEADERS, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    payload = soup.select_one('script#wix-warmup-data')
    if payload is None:
        raise ValueError('Wix warmup data was not found')
    events = find_events(json.loads(payload.get_text()))
    records = records_from_events(events)
    if not records:
        log_message(
            'No Adelphi Orchestra events found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return records


class AdelphiorchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='adelphiorchestra_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    AdelphiorchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
