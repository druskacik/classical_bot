import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://music.fsu.edu/'
CALENDAR_URL = f'{SOURCE_URL}events/calendar/'
FACET_API_URL = f'{SOURCE_URL}wp-json/facetwp/v1/refresh'
SOURCE = 'Florida State University College of Music'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def facet_payload(template, page):
    uri = 'events/calendar' if template == 'upcoming_events' else 'events/calendar/past'
    return {
        'action': 'facetwp_refresh',
        'data': {
            'facets': {
                'events_groups': [],
                'events_search': '',
                'events_types': [],
                'events_recitals': [],
                'events_locations': [],
                'events_calendar': [],
                'pager_by_numbers': [],
            },
            'frozen_facets': {},
            'http_params': {'get': {'_paged': str(page)}, 'uri': uri, 'url_vars': []},
            'template': template,
            'extras': {'sort': 'default'},
            'soft_refresh': 1,
            'is_bfcache': 1,
            'first_load': 0,
            'paged': str(page),
        },
    }


def fetch_listing_page(session, template, page):
    response = session.post(
        FACET_API_URL,
        json=facet_payload(template, page),
        headers={'Referer': CALENDAR_URL},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    soup = BeautifulSoup(data.get('template', ''), 'html.parser')
    cards = []
    for content in soup.select('.event-entry-content'):
        link = content.select_one('h3 a[href]')
        date_node = content.select_one('.label-md')
        venue_node = content.select_one('p.label-sm')
        if link and date_node and venue_node:
            cards.append({
                'title': clean_text(link.get_text(' ', strip=True)),
                'url': link['href'],
                'date_text': clean_text(date_node.get_text(' ', strip=True)),
                'venue': clean_text(venue_node.get_text(' ', strip=True)),
            })

    pager = BeautifulSoup(data.get('facets', {}).get('pager_by_numbers', ''), 'html.parser')
    pages = [int(node['data-page']) for node in pager.select('[data-page]') if node['data-page'].isdigit()]
    return cards, max(pages, default=1)


def parse_datetime(value):
    for pattern in ('%B %d, %Y %I:%M %p', '%B %d, %Y'):
        try:
            parsed = datetime.strptime(clean_text(value), pattern)
            return parsed.date().isoformat(), parsed.strftime('%H:%M') if '%I' in pattern else None
        except ValueError:
            pass
    return None, None


def parse_detail(session, card):
    try:
        response = session.get(card['url'], timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Event detail request failed',
            event='crawler_detail_failed',
            level='warning',
            url=card['url'],
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    soup = BeautifulSoup(response.text, 'html.parser')
    title_node = soup.select_one('main h1')
    details = soup.select_one('.events-details')
    date_node = details.select_one('.body-xl') if details else None
    venue_node = details.select_one('.fw-bold') if details else None
    address_node = venue_node.find_next_sibling() if venue_node else None
    description_node = soup.select_one('main .entry-content')

    title = clean_text(title_node.get_text(' ', strip=True)) if title_node else card['title']
    event_date, time_from = parse_datetime(
        date_node.get_text(' ', strip=True) if date_node else card['date_text']
    )
    venue = clean_text(venue_node.get_text(' ', strip=True)) if venue_node else card['venue']
    address = clean_text(address_node.get_text(' ', strip=True)) if address_node else ''
    city_match = re.search(r',\s*([^,]+),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b', address, re.I)
    city = clean_text(city_match.group(1)) if city_match else 'Tallahassee'

    if not title or not event_date or not venue or venue.casefold() == 'tbd' or not city:
        return None

    description = clean_text(description_node.get_text('\n', strip=True)) if description_node else ''
    return {
        'title': title,
        'date': event_date,
        'url': card['url'],
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    cards = []
    for template in ('upcoming_events', 'past_events'):
        first_cards, page_count = fetch_listing_page(session, template, 1)
        cards.extend(first_cards)
        for page in range(2, page_count + 1):
            page_cards, _ = fetch_listing_page(session, template, page)
            cards.extend(page_cards)

    unique_cards = {card['url']: card for card in cards}
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(parse_detail, session, card) for card in unique_cards.values()]
        for future in as_completed(futures):
            record = future.result()
            if record:
                records.append(record)

    if not records:
        log_message(
            'No valid events found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class MusicFsuEduCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='music_fsu_edu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    MusicFsuEduCrawler().run()


if __name__ == '__main__':
    main()
