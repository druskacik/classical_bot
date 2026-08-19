import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ncsymphony.org/'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/wp/v2/events'
SOURCE = 'North Carolina Symphony'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# These venue names are shown without a city on the event pages. The city is
# explicit in the page title/URL and in the orchestra's calendar presentation.
VENUE_CITIES = {
    'Alumni Gym at Elon University': 'Elon',
    'Brendle Recital Hall at Wake Forest University': 'Winston-Salem',
    'Duke Family Performance Hall at Davidson College': 'Davidson',
    'The Griffin Centre at Halifax Community College': 'Weldon',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', html.unescape(text).replace('\xa0', ' ')).strip()


def parse_datetime(value):
    value = clean_text(value)
    match = re.search(
        r'([A-Za-z]+\s+\d{1,2},\s+\d{4})\s+(\d{1,2}:\d{2}\s*[ap]m)',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        parsed = datetime.strptime(' '.join(match.groups()), '%B %d, %Y %I:%M %p')
    except ValueError:
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def extract_venue(details):
    icon = details.find('img', src=lambda value: value and 'venue.png' in value)
    if not icon:
        return ''
    box = icon.find_parent(class_='detail-box')
    venue_node = box.select_one('.details') if box else None
    return clean_text(venue_node)


def city_from_venue(venue):
    if venue in VENUE_CITIES:
        return VENUE_CITIES[venue]
    if ',' in venue:
        city = clean_text(venue.rsplit(',', 1)[-1])
        if re.fullmatch(r"[A-Za-z][A-Za-z .'-]+", city):
            return city
    return ''


def parse_event_page(url, session=None):
    session = session or requests.Session()
    response = session.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    details = soup.select_one('.event-details')
    if not details:
        return None

    title = clean_text(details.find('h1'))
    event_datetime = parse_datetime(details.select_one('.pre-top'))
    venue = extract_venue(details)
    city = city_from_venue(venue)
    if not title or not event_datetime or not venue or not city:
        log_message(
            'Skipping event with incomplete required fields',
            event='crawler_event_skipped',
            level='warning',
            url=url,
            has_title=bool(title),
            has_date=bool(event_datetime),
            has_venue=bool(venue),
            has_city=bool(city),
        )
        return None

    description_node = details.select_one('.elementor')
    description = clean_text(description_node) or None
    event_date, time_from = event_datetime
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event_urls(session):
    urls = []
    page = 1
    while True:
        response = session.get(
            EVENTS_API_URL,
            params={
                'per_page': 100,
                'page': page,
                'status': 'publish',
                '_fields': 'link',
            },
            timeout=45,
        )
        if response.status_code == 400 and page > 1:
            break
        response.raise_for_status()
        items = response.json()
        urls.extend(item.get('link') for item in items if item.get('link'))
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1
    return list(dict.fromkeys(urls))


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    urls = fetch_event_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(parse_event_page, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Event detail request failed',
                    event='crawler_detail_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    if not records:
        log_message(
            'No concert records found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_API_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class NcSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ncsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
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
    NcSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
