import json
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.irishbaroqueorchestra.com/'
EVENTS_URL = urljoin(SOURCE_URL, 'whatson')
SOURCE = 'Irish Baroque Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-IE,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def find_course_records(value):
    """Locate the Wix CMS collection without depending on volatile wrapper keys."""
    if isinstance(value, dict):
        courses = value.get('Courses')
        if isinstance(courses, dict) and courses and all(
            isinstance(item, dict) for item in courses.values()
        ):
            records = [
                item for item in courses.values()
                if isinstance(item.get('date1'), dict) and item.get('title')
            ]
            if records:
                return records
        for child in value.values():
            records = find_course_records(child)
            if records:
                return records
    elif isinstance(value, list):
        for child in value:
            records = find_course_records(child)
            if records:
                return records
    return []


def parse_datetime(value):
    raw = value.get('$date', '') if isinstance(value, dict) else str(value or '')
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?Z?', raw)
    if not match:
        return None, None
    try:
        datetime.strptime(match.group(1), '%Y-%m-%d')
    except ValueError:
        return None, None
    # Wix stores and displays these values as local wall-clock times despite the Z suffix.
    return match.group(1), match.group(2)


def normalize_city(value):
    city = clean_text(value)
    return re.sub(r'\s+\d+$', '', city).strip()


def infer_city(address, venue):
    city = normalize_city(address.get('city'))
    if city:
        return city

    venue_text = clean_text(venue)
    if re.search(r'\bKinsale\b', venue_text, re.I):
        return 'Kinsale'
    return ''


def parse_record(item):
    title = clean_text(item.get('title') or item.get('shortCourseDescription'))
    event_date, time_from = parse_datetime(item.get('date1'))
    venue = clean_text(item.get('director'))
    path = clean_text(item.get('link-courses-title'))
    address = item.get('address') if isinstance(item.get('address'), dict) else {}
    city = infer_city(address, venue)
    country_code = clean_text(address.get('country')).upper()
    url = urljoin(SOURCE_URL, path)

    if not all((title, event_date, path, venue, city, country_code)):
        return None
    if not re.fullmatch(r'[A-Z]{2}', country_code):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_text(item.get('longDescription')) or None,
    }


def parse_events_page(html):
    soup = BeautifulSoup(html, 'html.parser')
    warmup = soup.select_one('script#wix-warmup-data')
    if not warmup or not warmup.string:
        raise ValueError('Wix warmup data was not found')
    payload = json.loads(warmup.string)
    items = find_course_records(payload)
    if not items:
        raise ValueError('Wix Courses collection was not found')

    records = []
    for item in items:
        record = parse_record(item)
        if record:
            records.append(record)
        else:
            path = clean_text(item.get('link-courses-title'))
            log_message(
                'Skipped incomplete Irish Baroque Orchestra concert',
                event='crawler_item_skipped',
                level='warning',
                url=urljoin(SOURCE_URL, path) if path else EVENTS_URL,
                error_type='IncompleteEventData',
                error_message='Required date, title, URL, venue, city, or country is missing',
            )
    return records


class IrishBaroqueOrchestraComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='irishbaroqueorchestra_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(EVENTS_URL, headers=HEADERS, timeout=60)
        response.raise_for_status()
        records = parse_events_page(response.text)
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    IrishBaroqueOrchestraComCrawler().run()


if __name__ == '__main__':
    main()
