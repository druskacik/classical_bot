import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.camarada.org/'
SOURCE = 'Camarada'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
LOCAL_TIMEZONE = ZoneInfo('America/Los_Angeles')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', html.unescape(str(value)).replace('\xa0', ' ')).strip()


def event_datetime(milliseconds):
    if not isinstance(milliseconds, (int, float)):
        return None, None
    value = datetime.fromtimestamp(milliseconds / 1000, LOCAL_TIMEZONE)
    return value.date().isoformat(), value.strftime('%H:%M')


def venue_and_city(body):
    soup = BeautifulSoup(body or '', 'html.parser')
    paragraphs = [clean_text(node) for node in soup.select('.sqs-html-content p')]
    candidates = []
    for node in soup.select('.sqs-html-content p'):
        lines = [clean_text(part) for part in node.stripped_strings if clean_text(part)]
        text = ' '.join(lines)
        if re.search(r'\b(?:CA(?:\s+\d{5})?|California|Mexico|B\.C\.)\b', text, re.I):
            candidates.append(lines)

    for lines in reversed(candidates):
        text = ' '.join(lines)
        if re.search(r'\bTijuana\b', text, re.I):
            city, country = 'Tijuana', 'MX'
        else:
            match = re.search(
                r',\s*([A-Za-z][A-Za-z .\'-]+?),\s*(?:CA|California)\b', text
            )
            city = clean_text(match.group(1)) if match else ''
            country = 'US'
        venue_lines = [
            line for line in lines
            if not re.search(r'\d{3,}|\b(?:CA|California|Mexico|United States|B\.C\.)\b', line, re.I)
            and not re.search(r'\b(?:doors? open|concert begins?|tickets? issued)\b', line, re.I)
        ]
        venue = ', '.join(dict.fromkeys(venue_lines))
        if not venue:
            inline = re.search(
                r'(?:Concert Location:\s*)?(.+?),\s*\d+[A-Za-z]?\s+', text, re.I
            )
            venue = clean_text(inline.group(1)) if inline else ''
        if venue and city:
            return venue, city, country

    # The event editor's location fields are unused, but Camarada's calendar is
    # overwhelmingly local. Only use this fallback where the body names a known
    # home venue and does not indicate a tour.
    text = ' '.join(paragraphs)
    known = {
        'Mingei International Museum': ('San Diego', 'US'),
        'The San Diego Museum of Art': ('San Diego', 'US'),
        'San Diego Museum of Art': ('San Diego', 'US'),
        'The Conrad Prebys Performing Arts Center': ('La Jolla', 'US'),
        'UC San Diego Park & Market': ('San Diego', 'US'),
    }
    for venue, (city, country) in known.items():
        if venue.lower() in text.lower():
            return venue, city, country
    return '', '', ''


def parse_item(item):
    title = clean_text(item.get('title'))
    path = item.get('fullUrl')
    event_date, time_from = event_datetime(item.get('startDate'))
    venue, city, country_code = venue_and_city(item.get('body'))
    description = clean_text(BeautifulSoup(item.get('body') or '', 'html.parser')) or None
    if not all((title, path, event_date, venue, city, country_code)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, path),
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class CamaradaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='camarada_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        offset = None
        items = {}
        while True:
            params = {'format': 'json'}
            if offset is not None:
                params['offset'] = offset
            try:
                response = session.get(CALENDAR_URL, params=params, timeout=45)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Camarada calendar',
                    event='crawler_fetch_failed',
                    level='error',
                    url=response.url if 'response' in locals() else CALENDAR_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            page_items = [*(payload.get('upcoming') or []), *(payload.get('past') or [])]
            for item in page_items:
                if item.get('id'):
                    items[item['id']] = item

            pagination = payload.get('pagination') or {}
            next_offset = pagination.get('nextPageOffset')
            if not pagination.get('nextPage') or next_offset is None or next_offset == offset:
                break
            offset = next_offset

        records = []
        for item in items.values():
            record = parse_item(item)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping Camarada event with incomplete location data',
                    event='crawler_record_skipped',
                    level='warning',
                    url=urljoin(SOURCE_URL, item.get('fullUrl') or ''),
                )
        return records


def main():
    return CamaradaOrgCrawler().run()


if __name__ == '__main__':
    main()
