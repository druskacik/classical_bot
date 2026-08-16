import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.marylandsymphony.org/'
EVENTS_API = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Maryland Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    raw = html.unescape(str(value))
    text = (
        BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
        if '<' in raw
        else raw.strip()
    )
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def description_location(description):
    """Extract an explicitly advertised off-site location.

    Some MSO Lite records retain the Maryland Theatre in the API's venue field
    while their body starts with the actual community venue and address.
    """
    match = re.search(r'(?:^|\n)Live at\s+([^\n]+)', description, re.I)
    if not match:
        return '', ''
    venue = clean_text(match.group(1)).strip(' ,')
    tail = description[match.end():match.end() + 300]
    city_match = re.search(
        r'(?:^|\n)([A-Za-z][A-Za-z .\'-]+),\s*MD\s+\d{5}(?:-\d{4})?',
        tail,
        re.I,
    )
    city = clean_text(city_match.group(1)) if city_match else ''
    return venue, city


def known_description_location(description):
    known_venues = {
        'Hagerstown Community College': 'Hagerstown',
        'Hub City Vinyl': 'Hagerstown',
    }
    for venue, city in known_venues.items():
        if re.search(rf'(?:^|\n){re.escape(venue)}(?:\n|$)', description, re.I):
            return venue, city
    return '', ''


def make_record(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    start = clean_text(event.get('start_date'))
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2}):\d{2}', start)
    if not match:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    description = clean_text(event.get('description')) or None
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    body_venue, body_city = description_location(description or '')
    if body_venue:
        venue = body_venue
        city = body_city or city
    elif not venue or not city:
        body_venue, body_city = known_description_location(description or '')
        venue = venue or body_venue
        city = city or body_city

    if not title or not url or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': f'{match.group(2)}:{match.group(3)}',
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class MarylandSymphonySecureForceComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='marylandsymphony_secure_force_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            response = session.get(
                EVENTS_API,
                params={
                    'start_date': '2000-01-01 00:00:00',
                    'end_date': '2100-12-31 23:59:59',
                    'per_page': 50,
                    'page': page,
                },
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
            total_pages = int(payload.get('total_pages') or 1)
            for event in payload.get('events') or []:
                record = make_record(event)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Maryland Symphony event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=clean_text(event.get('url')),
                        error_type='IncompleteEventData',
                        error_message='Required title, date, URL, venue, or city is missing',
                    )
            page += 1

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['url']
            ),
        )


def main():
    MarylandSymphonySecureForceComCrawler().run()


if __name__ == '__main__':
    main()
