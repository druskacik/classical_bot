from datetime import date
from html import unescape
import re

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mnphil.org/'
API_URL = f'{SOURCE_URL}wp-json/my-calendar/v1/events'
SOURCE = 'Minnesota Philharmonic Orchestra'
CONCERT_CATEGORY_ID = '1'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if value is None:
        return ''
    raw = (
        unescape(str(value))
        .replace('\\r', '\n')
        .replace('\\\'', '\'')
        .replace('\\"', '"')
    )
    text = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    raw = clean_text(value).split(' ', 1)[0]
    try:
        return date.fromisoformat(raw).isoformat()
    except (TypeError, ValueError):
        return None


def parse_time(value):
    match = re.fullmatch(r'(\d{1,2}):(\d{2})(?::\d{2})?', clean_text(value))
    if not match:
        return None
    hour, minute = map(int, match.groups())
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def event_categories(event):
    return {
        str(category.get('category_id'))
        for category in event.get('categories') or []
        if isinstance(category, dict)
    }


def event_location(event):
    location = event.get('location') or {}
    venue = clean_text(
        location.get('location_label') or event.get('event_label')
    )
    city = clean_text(
        location.get('location_city') or event.get('event_city')
    )
    return venue, city


def event_url(event):
    post_id = clean_text(event.get('event_post'))
    if post_id.isdigit() and int(post_id) > 0:
        return f'{SOURCE_URL}?p={post_id}'
    event_id = clean_text(event.get('event_id'))
    if event_id.isdigit() and int(event_id) > 0:
        return f'{SOURCE_URL}events/?mc_id={event_id}'
    return ''


def parse_event(event):
    if CONCERT_CATEGORY_ID not in event_categories(event):
        return None

    title = clean_text(event.get('event_title'))
    event_date = parse_date(event.get('occur_begin') or event.get('event_begin'))
    url = event_url(event)
    venue, city = event_location(event)
    if not all((title, event_date, url, venue, city)):
        return None

    description = clean_text(
        event.get('event_desc') or event.get('event_short')
    ) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(event.get('event_time')),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class MnphilOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mnphil_org',
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
        response = requests.get(
            API_URL,
            params={
                'from': '1993-01-01',
                'to': '2100-12-31',
                'category': CONCERT_CATEGORY_ID,
            },
            headers=HEADERS,
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError('Minnesota Philharmonic events API returned an unexpected response')

        records = []
        for events in payload.values():
            if not isinstance(events, list):
                continue
            for event in events:
                if not isinstance(event, dict):
                    continue
                record = parse_event(event)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Minnesota Philharmonic concert',
                        event='crawler_item_skipped',
                        level='warning',
                        url=event_url(event) or API_URL,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, URL, venue, or city is missing',
                    )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    MnphilOrgCrawler().run()


if __name__ == '__main__':
    main()
