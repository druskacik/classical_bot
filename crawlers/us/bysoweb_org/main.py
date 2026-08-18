import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bysoweb.org/'
EVENTS_URL = f'{SOURCE_URL}events/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/performances-events'
SOURCE = 'Boston Youth Symphony Orchestras'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUES = {
    'sanders theatre': ('Sanders Theatre at Harvard University', 'Cambridge'),
    'tsai performance center': ('Tsai Performance Center at Boston University', 'Boston'),
    'symphony hall': ('Symphony Hall', 'Boston'),
    'arlington high school': ('Arlington High School', 'Arlington'),
    "boston university's college of fine arts concert hall": (
        "Boston University's College of Fine Arts Concert Hall",
        'Boston',
    ),
    'faneuil hall': ('Faneuil Hall', 'Boston'),
    'cary memorial hall': ('Cary Memorial Hall', 'Lexington'),
    'museum of science': ('Museum of Science', 'Boston'),
    'kresge auditorium': ('Kresge Auditorium at MIT', 'Cambridge'),
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), '%b %d, %Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(value):
    value = clean_text(value).replace('.', ':').upper()
    for pattern in ('%I:%M%p', '%I:%M %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_location(value):
    location = clean_text(value).lstrip('|').strip()
    lowered = location.lower()
    for marker, result in VENUES.items():
        if marker in lowered:
            return result
    return '', ''


def flatten_payload(target, prefix, value):
    if isinstance(value, dict):
        for key, nested in value.items():
            flatten_payload(target, f'{prefix}[{key}]', nested)
    elif isinstance(value, list):
        target[f'{prefix}[]'] = value
    elif value is not None:
        if isinstance(value, bool):
            value = str(value).lower()
        target[prefix] = str(value)


def load_all_cards(session, listing_soup):
    cards = list(listing_soup.select('.jet-listing-grid__item[data-post-id]'))
    grid = listing_soup.select_one('.jet-listing-grid__items[data-nav]')
    if not grid:
        return cards

    try:
        navigation = json.loads(grid['data-nav'])
        pages = int(grid.get('data-pages', '1'))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return cards

    for page in range(2, pages + 1):
        payload = {'action': 'jet_engine_ajax', 'handler': 'listing_load_more'}
        flatten_payload(payload, 'query', navigation['query'])
        flatten_payload(payload, 'widget_settings', navigation['widget_settings'])
        payload.update({
            'page_settings[post_id]': 'false',
            'page_settings[queried_id]': grid.get('data-queried-id', ''),
            'page_settings[element_id]': 'c975fc7',
            'page_settings[page]': str(page),
            'listing_type': 'false',
            'isEditMode': 'false',
            'addedPostCSS[]': str(grid.get('data-listing-id', '2409308')),
        })
        response = session.post(
            f'{EVENTS_URL}?nocache=1',
            data=payload,
            headers={'Referer': EVENTS_URL},
            timeout=45,
        )
        response.raise_for_status()
        html = response.json().get('data', {}).get('html', '')
        cards.extend(BeautifulSoup(html, 'html.parser').select(
            '.jet-listing-grid__item[data-post-id]'
        ))
    return cards


def parse_card(card, urls):
    heading = card.select_one('h5')
    date_node = heading.select_one('.descriptor') if heading else None
    fields = card.select('.jet-listing-dynamic-field__content')
    if not heading or not date_node or len(fields) < 2:
        return None

    event_date = parse_date(date_node.get_text(' ', strip=True))
    heading_text = clean_text(heading.get_text(' ', strip=True))
    title = clean_text(re.sub(r'^.*?\s*/\s*', '', heading_text, count=1))
    time_from = parse_time(fields[0].get_text(' ', strip=True))
    venue, city = parse_location(fields[1].get_text(' ', strip=True))
    url = urls.get(str(card.get('data-post-id')), '')
    if not all((title, event_date, url, venue, city)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    listing_response = session.get(EVENTS_URL, timeout=45)
    listing_response.raise_for_status()
    cards = load_all_cards(session, BeautifulSoup(listing_response.text, 'html.parser'))

    api_response = session.get(API_URL, params={'per_page': 100, 'page': 1}, timeout=45)
    api_response.raise_for_status()
    urls = {str(item['id']): item['link'] for item in api_response.json()}

    records = []
    for card in cards:
        record = parse_card(card, urls)
        if record:
            records.append(record)

    if not records:
        log_message(
            'No valid performance records found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class BysowebOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bysoweb_org',
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
    BysowebOrgCrawler().run()


if __name__ == '__main__':
    main()
