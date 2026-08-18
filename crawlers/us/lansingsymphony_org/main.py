import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lansingsymphony.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
ARCHIVE_URL = urljoin(SOURCE_URL, 'events/archive')
SOURCE = 'Lansing Symphony Orchestra'

# First-party series whose advertised events are live orchestral, chamber,
# family, or orchestral crossover performances. Jazz and lecture series are
# deliberately omitted.
SERIES_IDS = ('1', '2', '3', '6', '24', '25', '26')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUE_CITIES = {
    'Wharton Center': 'East Lansing',
    'Wharton Center for Performing Arts': 'East Lansing',
    'Molly Grove Chapel': 'Lansing',
    'Robin Theatre': 'Lansing',
    'The Robin Theatre': 'Lansing',
    'Delta Township District Library': 'Lansing',
}

CITY_RE = re.compile(
    r'\b(East Lansing|Lansing|Haslett|DeWitt|Okemos|Holt|Grand Ledge|'
    r'Williamston|Mason|Charlotte)\b(?:,?\s*MI)?',
    re.IGNORECASE,
)


def clean_text(value, separator=' '):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text(separator, strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text(separator, strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    if separator == '\n':
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r' *\n *', '\n', text)
        return re.sub(r'\n{3,}', '\n\n', text).strip()
    return re.sub(r'\s+', ' ', text).strip()


def parse_time(value):
    value = clean_text(value).upper().replace('.', '')
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def location_parts(raw_location):
    location = clean_text(raw_location)
    if not location or re.search(r'on\s*demand|digital concert|online', location, re.I):
        return '', ''

    city_match = CITY_RE.search(location)
    city = city_match.group(1).title() if city_match else ''
    city = {'Dewitt': 'DeWitt'}.get(city, city)

    venue = location
    venue = re.sub(r'\s*:\s*\d+\s+.*$', '', venue)
    venue = re.sub(r'\s*\(\s*\d+\s+.*$', '', venue)
    venue = re.sub(
        r'\s+\d+\s+[A-Za-z0-9 .\'-]+(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Ln|Lane)\.?\b.*$',
        '',
        venue,
        flags=re.I,
    )
    venue = re.sub(r'\s*,?\s*(?:East Lansing|Lansing|Haslett|DeWitt|Okemos|Holt|Grand Ledge|Williamston|Mason|Charlotte)\s*,?\s*MI(?:\s+\d{5})?.*$', '', venue, flags=re.I)
    venue = venue.strip(' ,-:()')

    if not city:
        city = VENUE_CITIES.get(venue, '')
    if not city:
        for marker, marker_city in (
            ('East Lansing', 'East Lansing'), ('Haslett', 'Haslett'),
            ('DeWitt', 'DeWitt'), ('Lansing', 'Lansing'),
        ):
            if marker.lower() in venue.lower():
                city = marker_city
                break
    return venue, city


def listing_items(html):
    soup = BeautifulSoup(html, 'html.parser')
    items = []
    for row in soup.select('.views-row .event-object'):
        link = row.select_one('.event-item__title a[href]')
        date_node = row.select_one('.date-block time[datetime]')
        location_node = row.select_one('.event-item__location')
        if not link or not date_node or not location_node:
            continue
        event_date = clean_text(date_node.get('datetime'))[:10]
        try:
            event_date = datetime.strptime(event_date, '%Y-%m-%d').date().isoformat()
        except ValueError:
            continue
        venue, city = location_parts(location_node)
        if not venue or not city:
            continue
        time_node = row.select_one('.event-detail__with-icon time')
        items.append({
            'title': clean_text(link),
            'date': event_date,
            'url': urljoin(SOURCE_URL, link.get('href')),
            'time_from': parse_time(time_node) if time_node else None,
            'venue': venue,
            'city': city,
        })

    pages = []
    for link in soup.select('.pager a[href]'):
        href = urljoin(SOURCE_URL, link.get('href'))
        if href not in pages:
            pages.append(href)
    return items, pages


def detail_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    parts = []
    for selector in (
        '.event__description', '.event__conductor', '.event__personnel',
        '.event__program',
    ):
        for node in soup.select(selector):
            text = clean_text(node, separator='\n')
            if text and text not in parts:
                parts.append(text)
    return '\n\n'.join(parts) or None


def fetch_description(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return detail_description(response.text)


class LansingSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lansingsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records_by_key = {}

        for base_url in (EVENTS_URL, ARCHIVE_URL):
            for series_id in SERIES_IDS:
                first_url = f'{base_url}?field_series_target_id={series_id}'
                pending = [first_url]
                seen_pages = set()
                while pending:
                    page_url = pending.pop(0)
                    if page_url in seen_pages:
                        continue
                    seen_pages.add(page_url)
                    response = session.get(page_url, timeout=45)
                    response.raise_for_status()
                    items, pages = listing_items(response.text)
                    for item in items:
                        key = (
                            item['title'], item['date'], item['time_from'],
                            item['venue'], item['city'],
                        )
                        records_by_key[key] = item
                    pending.extend(page for page in pages if page not in seen_pages)

        records = list(records_by_key.values())
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(fetch_description, session, record['url']): record
                for record in records
            }
            for future in as_completed(futures):
                record = futures[future]
                try:
                    record['description'] = future.result()
                except requests.RequestException as error:
                    record['description'] = None
                    log_message(
                        'Failed to scrape Lansing Symphony event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=record['url'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue'],
            ),
        )


def main():
    LansingSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
