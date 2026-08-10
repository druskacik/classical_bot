import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.gewandhausorchester.de/'
SOURCE = 'Gewandhaus Leipzig'
AJAX_URL = urljoin(SOURCE_URL, 'ajax-page-content/')
PAGE_SIZE = 6
MAX_WORKERS = 16

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

# The public calendar covers the Gewandhaus, Leipzig Opera and St Thomas
# Church. Avoid applying this home-city default to explicitly named tour stops.
LEIPZIG_VENUE_MARKERS = (
    'gewandhaus',
    'grosser saal',
    'mendelssohn-saal',
    'mendelssohn-foyer',
    'hauptfoyer',
    'oper leipzig',
    'opernhaus',
    'thomaskirche',
    'barlach-ebene',
    'chorprobensaal',
    'mendelssohn-haus',
    'musiksalon des mendelssohn-hauses',
    'open-air-bühne im rosental',
    'evangelisch reformierte kirche',
    'musikpavillon',
)

TOUR_VENUES = {
    'barbican centre': ('London', 'GB'),
    'beethovenhalle': ('Bonn', 'DE'),
    'elbphilharmonie': ('Hamburg', 'DE'),
    'great amber concert hall': ('Liepāja', 'LV'),
    'hong kong cultural centre': ('Hong Kong', 'HK'),
    'kölner philharmonie': ('Köln', 'DE'),
    'kultur casino bern': ('Bern', 'CH'),
    'latvijas nacionala opera': ('Riga', 'LV'),
    'musikverein wien': ('Wien', 'AT'),
    'philharmonie de paris': ('Paris', 'FR'),
    'philharmonie essen': ('Essen', 'DE'),
    'seoul arts center': ('Seoul', 'KR'),
    'tokyo metropolitan theatre': ('Tokyo', 'JP'),
    'tonhalle zürich': ('Zürich', 'CH'),
    'victoria hall': ('Genève', 'CH'),
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(offset):
    last_error = None
    for _attempt in range(3):
        try:
            response = requests.get(
                AJAX_URL,
                params={
                    'tx_csconcerts_pi1[action]': 'listConcerts',
                    'tx_csconcerts_pi1[controller]': 'Concert',
                    'tx_csconcerts_pi1[offset]': offset,
                },
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            return BeautifulSoup(response.text, 'html.parser')
        except requests.RequestException as error:
            last_error = error
    raise last_error


def page_has_events(page_number):
    return bool(get_page(page_number * PAGE_SIZE).select('.js-concert-item'))


def last_page_number():
    """Find the final populated page without relying on a cached cHash URL."""
    lower = 0
    upper = 1
    while page_has_events(upper):
        lower = upper
        upper *= 2

    while lower + 1 < upper:
        middle = (lower + upper) // 2
        if page_has_events(middle):
            lower = middle
        else:
            upper = middle
    return lower


def description_from_item(item, title):
    container = item.select_one('.event-teaser__short-description-link')
    if not container:
        return None

    parts = []
    for element in container.select('p'):
        text = clean_text(element)
        if text and text != title and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def resolve_location(venue):
    normalized = venue.casefold()
    if 'leipzig' in normalized or any(
        marker in normalized for marker in LEIPZIG_VENUE_MARKERS
    ):
        return 'Leipzig', 'DE'
    return TOUR_VENUES.get(normalized, (None, None))


def make_record(item):
    visible_date = item.select_one('time[datetime]')
    title_element = item.select_one('[id^="csConcertsTitle"]')
    detail_link = item.select_one('a[title="Details anzeigen"][href]')
    accessible_date = item.select_one('[id^="csConcertsDate"]')
    if not visible_date or not title_element or not detail_link or not accessible_date:
        return None

    try:
        event_date = date.fromisoformat(visible_date.get('datetime', '')).isoformat()
    except ValueError:
        return None

    title = clean_text(title_element)
    title = re.sub(r'^Veranstaltung:\s*', '', title, flags=re.IGNORECASE)
    date_text = clean_text(accessible_date)
    time_match = re.search(r',\s*(\d{1,2}):(\d{1,2})\s*Uhr', date_text)
    venue_match = re.search(r'Ort:\s*(.+?)\.?$', date_text, flags=re.DOTALL)
    venue = clean_text(venue_match.group(1)) if venue_match else ''
    city, country_code = resolve_location(venue)
    url = urljoin(SOURCE_URL, detail_link.get('href', ''))
    if not title or not venue or not city or not url:
        return None

    time_from = None
    if time_match:
        hour, minute = map(int, time_match.groups())
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            time_from = f'{hour:02d}:{minute:02d}'

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description_from_item(item, title),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    final_page = last_page_number()
    records = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(get_page, page * PAGE_SIZE): page
            for page in range(final_page + 1)
        }
        for future in as_completed(futures):
            page = futures[future]
            try:
                soup = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert listing page',
                    event='crawler_page_failed',
                    level='warning',
                    url=f'{AJAX_URL}?offset={page * PAGE_SIZE}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            for item in soup.select('.js-concert-item'):
                record = make_record(item)
                if record:
                    records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class GewandhausorchesterDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gewandhausorchester_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    GewandhausorchesterDeCrawler().run()


if __name__ == '__main__':
    main()
