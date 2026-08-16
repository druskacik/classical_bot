import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://marinsymphony.org/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/concerts-events'
SOURCE = 'Marin Symphony'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+'
    r'([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})'
    r'(?:,?\s+(\d{1,2}(?::\d{2})?\s*[ap]m))?',
    re.IGNORECASE,
)
CITY_RE = re.compile(
    r'\b(San Rafael|Novato|Mill Valley|Larkspur|Corte Madera|Fairfax|'
    r'San Anselmo|Sausalito|Tiburon|Ross|Kentfield|Greenbrae|Belvedere)\b',
    re.IGNORECASE,
)
VENUE_RULES = [
    (r'Mt\.?\s*Tam(?:alpais)?\s+(?:United\s+)?Methodist', 'Mt. Tamalpais United Methodist Church', 'Mill Valley'),
    (r'Rodef Sholom', 'Congregation Rodef Sholom', 'San Rafael'),
    (r'(?:College of Marin,?\s*)?(?:James Dunn Theatre|Performing Arts)', 'College of Marin, James Dunn Theatre', 'Kentfield'),
    (r'Marin Center Veterans[’\']? Memorial Auditorium', "Marin Center Veterans' Memorial Auditorium", 'San Rafael'),
    (r'(?:Exhibit Hall,? Marin Center|Marin Center Exhibit Hall)', 'Marin Center Exhibit Hall', 'San Rafael'),
    (r'(?:Saint|St\.?)\s*Raphael(?: Church)?', 'St. Raphael Church', 'San Rafael'),
    (r'Marin School of the Arts', 'Marin School of the Arts', 'Novato'),
    (r'Corte Madera Community Center', 'Corte Madera Community Center', 'Corte Madera'),
    (r'Mill Valley Recreation Center', 'Mill Valley Recreation Center', 'Mill Valley'),
    (r'Marin Country Mart', 'Marin Country Mart', 'Larkspur'),
]


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', unescape(text).replace('\xa0', ' ')).strip()


def parse_occurrence(value):
    match = DATE_TIME_RE.search(clean_text(value))
    if not match:
        return None
    month, day, year, event_time = match.groups()
    try:
        event_date = datetime.strptime(
            f'{month} {day} {year}', '%b %d %Y'
        ).date().isoformat()
    except ValueError:
        return None

    time_from = None
    if event_time:
        for pattern in ('%I:%M %p', '%I %p'):
            try:
                time_from = datetime.strptime(
                    event_time.upper().replace('.', ''), pattern
                ).strftime('%H:%M')
                break
            except ValueError:
                pass
    return event_date, time_from


def city_from_address(value):
    match = CITY_RE.search(clean_text(value))
    return match.group(1).title() if match else ''


def venues_from_location(value):
    matches = []
    for pattern, venue, city in VENUE_RULES:
        match = re.search(pattern, value, re.IGNORECASE)
        if match:
            matches.append((match.start(), venue, city))
    return [(venue, city) for _, venue, city in sorted(matches)]


def api_events(session):
    records = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'date',
                'order': 'desc',
                '_fields': 'id,link,title,concert_category',
            },
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        records.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return records
        page += 1


def parse_event_page(event, html):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    if not main:
        return []

    title = clean_text(event.get('title', {}).get('rendered'))
    url = event.get('link', '')
    widgets = main.select('.elementor-widget')

    date_widget_index = next(
        (index for index, widget in enumerate(widgets)
         if clean_text(widget).lower().startswith('dates & time')),
        None,
    )
    location_widget_index = next(
        (index for index, widget in enumerate(widgets)
         if clean_text(widget).lower().startswith('location details')),
        None,
    )
    if date_widget_index is None or location_widget_index is None:
        return []

    occurrences = []
    for widget in widgets[date_widget_index + 1:location_widget_index]:
        occurrence = parse_occurrence(widget.get_text(' ', strip=True))
        if occurrence and occurrence not in occurrences:
            occurrences.append(occurrence)

    location_values = []
    for widget in widgets[location_widget_index + 1:]:
        widget_type = widget.get('data-widget_type', '')
        if widget_type == 'text-editor.default':
            value = clean_text(widget)
            if value:
                location_values.append(value)
        elif location_values:
            break

    location_text = ' '.join(location_values)
    venue_locations = venues_from_location(location_text)
    if not venue_locations and len(location_values) >= 2:
        city = city_from_address(location_values[0])
        if city:
            venue_locations = [(location_values[-1], city)]

    description = None
    for widget in widgets[:date_widget_index]:
        if widget.get('data-widget_type') == 'jet-listing-dynamic-field.default':
            value = clean_text(widget)
            if value and not value.lower().startswith(('reserved seating', 'ticket')):
                description = value
                break

    if not title or not url or not occurrences or not venue_locations:
        return []

    records = []
    for index, (event_date, time_from) in enumerate(occurrences):
        venue, city = venue_locations[index] if len(venue_locations) == len(occurrences) else venue_locations[0]
        records.append({
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
        })
    return records


def fetch_event(event):
    response = requests.get(event['link'], headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event_page(event, response.text)


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    events = api_events(session)
    records = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_event, event): event for event in events}
        for future in as_completed(futures):
            event = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Unable to fetch concert detail',
                    event='crawler_detail_failed',
                    level='warning',
                    url=event.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No valid concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class MarinSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='marinsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    MarinSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
