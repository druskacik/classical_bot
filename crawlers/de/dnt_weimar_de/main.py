import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.dnt-weimar.de/'
CALENDAR_API = urljoin(SOURCE_URL, 'ext/ajax/spielplan_ajax.php')
SOURCE = 'Deutsches Nationaltheater und Staatskapelle Weimar'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

# The DNT calendar also includes a few clearly named festival locations outside
# Weimar. All other regular DNT venues belong to its home-city calendar.
EXTERNAL_CITY_MARKERS = {
    'apolda': 'Apolda',
    'ilmenau': 'Ilmenau',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def add_months(value, offset):
    month_index = value.year * 12 + value.month - 1 + offset
    return f'{month_index // 12:04d}-{month_index % 12 + 1:02d}'


def fetch_month(month):
    response = requests.get(
        CALENDAR_API,
        params={'month': month},
        headers=HEADERS,
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        return []
    if not isinstance(payload, dict):
        raise ValueError(f'Unexpected calendar response for {month}')

    # Invalid/past month requests are silently replaced with the current month.
    # Only accept dates belonging to the month that was actually requested.
    return [
        event
        for event_date, events in payload.items()
        if event_date.startswith(f'{month}-') and isinstance(events, list)
        for event in events
        if isinstance(event, dict)
    ]


def event_city(event, venue):
    detail = event.get('termin') or {}
    location_text = ' '.join(
        filter(None, (venue, clean_text(detail.get('ter_gastspiel_ort'))))
    )
    lowered = location_text.lower()
    for marker, city in EXTERNAL_CITY_MARKERS.items():
        if marker in lowered:
            return city

    # A touring record must not inherit the institution's home city. The API
    # currently exposes no such records, but skip any future ambiguous one.
    if detail.get('stu_unterwegs') or detail.get('ter_gastspiel_ort'):
        return None
    return 'Weimar'


def description_from(event):
    detail = event.get('termin') or {}
    parts = []
    for key in (
        'stu_untertitel1',
        'stu_untertitel2',
        'stu_autor',
        'stu_spielplan_text',
        'stu_kurztext',
        'stu_langtext',
        'stu_extratext',
    ):
        value = clean_text(detail.get(key))
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def make_record(event):
    detail = event.get('termin') or {}
    title = clean_text(event.get('stu_titel') or detail.get('stu_titel'))
    venue = clean_text(event.get('ort') or detail.get('ort'))
    start = event.get('ter_datum') or detail.get('ter_datum')
    link = event.get('link') or detail.get('stu_external_link')
    if not title or not venue or not start or not link:
        return None

    try:
        starts_at = datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None

    city = event_city(event, venue)
    if not city:
        return None
    url = urljoin(SOURCE_URL, link)
    if not url.startswith(('http://', 'https://')):
        return None

    show_time = event.get('ter_show_time', detail.get('ter_show_time', 1))
    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': starts_at.strftime('%H:%M') if show_time else None,
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': description_from(event),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    # Published DNT seasons extend less than two years ahead. Querying 24
    # calendar months covers the full announced catalogue and future rollover.
    months = [add_months(date.today(), offset) for offset in range(24)]
    events = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_month, month): month for month in months}
        for future in as_completed(futures):
            month = futures[future]
            try:
                events.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape calendar month',
                    event='crawler_page_failed',
                    level='warning',
                    url=f'{CALENDAR_API}?month={month}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = [record for event in events if (record := make_record(event))]
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class DntWeimarDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='dnt_weimar_de',
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
        dedupe_subset=['date', 'time_from', 'url', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    DntWeimarDeCrawler().run()


if __name__ == '__main__':
    main()
