import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://boisephil.org/'
SOURCE = 'Boise Philharmonic'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/bp_event'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/html;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUE_CITIES = {
    'morrison center': 'Boise',
    'shrine social club': 'Boise',
    'cathedral of the rockies': 'Boise',
    'borah high school': 'Boise',
    'capital high school': 'Boise',
    'idaho shakespeare festival': 'Boise',
    'bsu special events center': 'Boise',
    'bsu spec': 'Boise',
    'brandt center': 'Nampa',
    'nampa public library': 'Nampa',
    'nampa pubic library': 'Nampa',
    'boise public library': 'Boise',
    'library! at bown crossing': 'Boise',
    'meridian library': 'Meridian',
    'the grove hotel': 'Boise',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(?:at\s+)?(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)\b', value, re.I)
    if not match:
        return None
    normalized = match.group(1).replace('.', '').replace(' ', '').upper()
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def normalize_venue(value):
    venue = clean_text(value).strip(' ,|-')
    return venue or None


def city_for_venue(venue):
    normalized = clean_text(venue).lower()
    for name, city in VENUE_CITIES.items():
        if name in normalized:
            return city
    match = re.search(r',\s*(Boise|Nampa|Meridian)(?:\s*,\s*ID)?$', normalized, re.I)
    return match.group(1).title() if match else None


def infer_location(description):
    lowered = description.lower()
    for name, city in VENUE_CITIES.items():
        if name in lowered:
            # Preserve the site's most useful public venue names.
            venue = {
                'nampa pubic library': 'Nampa Public Library',
                'meridian library': 'Meridian Library – Pinnacle',
                'boise public library': 'Boise Public Library',
                'library! at bown crossing': 'Library! at Bown Crossing',
                'bsu special events center': 'BSU Special Events Center',
            }.get(name, name.title())
            return venue, city
    return None, None


def occurrence_data(page_html, description):
    soup = BeautifulSoup(page_html, 'html.parser')
    occurrences = []
    current = None
    for node in soup.select('.evt-date, .evt-time, .evt-venue'):
        classes = node.get('class', [])
        if 'evt-date' in classes:
            if current:
                occurrences.append(current)
            current = {'date': parse_date(node.get_text(' ', strip=True))}
        elif current is not None and 'evt-time' in classes:
            current['time_from'] = parse_time(node.get_text(' ', strip=True))
        elif current is not None and 'evt-venue' in classes:
            current['venue'] = normalize_venue(node.get_text(' ', strip=True))
    if current:
        occurrences.append(current)

    fallback_venue, fallback_city = infer_location(description)
    for occurrence in occurrences:
        occurrence.setdefault('time_from', None)
        if occurrence.get('venue'):
            occurrence['city'] = city_for_venue(occurrence['venue'])
        else:
            occurrence['venue'] = fallback_venue
            occurrence['city'] = fallback_city
    return occurrences


def build_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def fetch_detail(url):
    response = build_session().get(url, timeout=45)
    response.raise_for_status()
    return response.text


def scrape_concerts(session=None):
    session = session or build_session()
    response = session.get(API_URL, params={'per_page': 100, 'page': 1}, timeout=45)
    response.raise_for_status()
    events = response.json()

    usable_events = []
    for event in events:
        url = event.get('link', '')
        title = clean_text(event.get('title', {}).get('rendered'))
        description = clean_text(event.get('content', {}).get('rendered')) or None
        if title and url:
            usable_events.append((event, title, description, url))

    detail_pages = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(fetch_detail, item[3]): item
            for item in usable_events
        }
        for future in as_completed(futures):
            event, title, description, url = futures[future]
            try:
                detail_pages[event['id']] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Event detail request failed',
                    event='crawler_detail_request_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = []
    for event, title, description, url in usable_events:
        page_html = detail_pages.get(event['id'])
        if not page_html:
            continue
        for occurrence in occurrence_data(page_html, description or ''):
            if not occurrence.get('date') or not occurrence.get('venue') or not occurrence.get('city'):
                continue
            records.append({
                'title': title,
                'date': occurrence['date'],
                'url': url,
                'time_from': occurrence['time_from'],
                'venue': occurrence['venue'],
                'city': occurrence['city'],
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    if not records:
        log_message(
            'No valid event occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class BoisePhilOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='boisephil_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
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
        return scrape_concerts()


def main():
    BoisePhilOrgCrawler().run()


if __name__ == '__main__':
    main()
