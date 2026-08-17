import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://shop.slso.org/'
SOURCE = 'St. Louis Symphony Orchestra'
CALENDAR_URL = 'https://slso.org/get-tickets/event-calendar/'
AJAX_URL = 'https://slso.org/wp-admin/admin-ajax.php'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# The calendar explicitly names these venues but does not print their cities.
VENUE_CITIES = {
    'Art Hill in Forest Park': 'St. Louis',
    'J. Scheidegger Center for the Arts, Lindenwood University': 'St. Charles',
    'Powell Hall': 'St. Louis',
    'The Sheldon': 'St. Louis',
    'The Sheldon Concert Hall': 'St. Louis',
    'The Pulitzer Arts Foundation': 'St. Louis',
    'Pulitzer Arts Foundation': 'St. Louis',
}

OCCURRENCE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4}),\s*'
    r'(\d{1,2}(?::\d{2})?\s*[ap]m)',
    re.I,
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def parse_occurrences(value):
    occurrences = []
    for month, day, year, event_time in OCCURRENCE_RE.findall(clean_text(value)):
        try:
            parsed = datetime.strptime(
                f'{month} {day} {year} {event_time.upper()}', '%b %d %Y %I:%M%p'
            )
        except ValueError:
            try:
                parsed = datetime.strptime(
                    f'{month} {day} {year} {event_time.upper()}', '%b %d %Y %I%p'
                )
            except ValueError:
                continue
        occurrences.append((parsed.date().isoformat(), parsed.strftime('%H:%M')))
    return occurrences


def listing_items(session):
    response = session.post(
        AJAX_URL,
        data={
            'action': 'filter_events_by_tag',
            'promo': '',
            'startdate': '',
            'enddate': '',
            'exclude[]': '34177',
        },
        timeout=60,
    )
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser').select('li.event-listing__item')


def detail_description(session, url):
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Event detail request failed',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    soup = BeautifulSoup(response.text, 'html.parser')
    sections = []
    for node in soup.select('.copy-sidebar__wrap, .event-content__copy'):
        text = clean_text(node)
        if text and text not in sections:
            sections.append(text)
    return '\n\n'.join(sections) or None


def parse_listing_item(item):
    content = item.select_one('a.event-listing__content')
    title_node = item.select_one('.event-listing__title')
    if not content or not title_node or not content.get('href'):
        return None

    title_parts = [clean_text(title_node)]
    subtitle = clean_text(item.select_one('.event-listing__subtitle'))
    if subtitle:
        title_parts.append(subtitle)
    title = ': '.join(part for part in title_parts if part)
    url = content['href']

    venue_node = item.select_one('.event-listing__location strong')
    venue = clean_text(venue_node)
    city = VENUE_CITIES.get(venue)
    occurrences = parse_occurrences(item.select_one('.event-listing__buttons'))
    if not title or not venue or not city or not occurrences:
        return None

    intro = clean_text(item.select_one('.event-listing__intro')) or None
    return {
        'title': title,
        'url': url,
        'venue': venue,
        'city': city,
        'occurrences': occurrences,
        'intro': intro,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    parsed_items = []
    for item in listing_items(session):
        parsed = parse_listing_item(item)
        if parsed:
            parsed_items.append(parsed)

    descriptions = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(detail_description, session, item['url']): item['url']
            for item in parsed_items
        }
        for future in as_completed(futures):
            descriptions[futures[future]] = future.result()

    records = []
    for item in parsed_items:
        description = descriptions.get(item['url']) or item['intro']
        for event_date, time_from in item['occurrences']:
            records.append({
                'title': item['title'],
                'date': event_date,
                'url': item['url'],
                'time_from': time_from,
                'venue': item['venue'],
                'city': item['city'],
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
            url=CALENDAR_URL,
            record_count=0,
        )
    return sorted(records, key=lambda row: (row['date'], row['time_from'], row['title']))


class ShopSlsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='shop_slso_org',
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
    ShopSlsoOrgCrawler().run()


if __name__ == '__main__':
    main()
