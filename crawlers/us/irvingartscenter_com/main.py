import html
import json
import re
from datetime import date, datetime
from urllib.parse import parse_qsl

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.irvingartscenter.com/'
CALENDAR_URL = f'{SOURCE_URL}tickets-events/'
AJAX_URL = f'{SOURCE_URL}wp-admin/admin-ajax.php'
SOURCE = 'Irving Arts Center'
CITY = 'Irving'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
}


def clean_text(value):
    if not value:
        return ''
    raw = html.unescape(str(value))
    text = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True) if '<' in raw else raw
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def valid_date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return None


def parse_time(value):
    if not value or 'T' not in value:
        return None
    raw = value.split('T', 1)[1][:5]
    try:
        return datetime.strptime(raw, '%H:%M').strftime('%H:%M')
    except ValueError:
        return None


def iter_event_json(markup):
    soup = BeautifulSoup(markup, 'html.parser')
    times = {}
    for article in soup.select('article.mec-event-article'):
        link = article.select_one('.mec-event-title a[href]')
        start = article.select_one('.mec-start-time')
        if link and start:
            times[link.get('href')] = start.get_text(' ', strip=True)
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                listed_time = times.get(item.get('url'))
                if listed_time:
                    item['_listed_time'] = listed_time
                yield item


def make_record(event):
    title = clean_text(event.get('name'))
    event_date = valid_date(str(event.get('startDate') or '')[:10])
    url = clean_text(event.get('url'))
    location = event.get('location') or {}
    venue = clean_text(location.get('name')) if isinstance(location, dict) else ''
    address = clean_text(location.get('address')) if isinstance(location, dict) else ''

    # The selected calendar is the venue's own Irving programme. Its structured
    # addresses consistently identify Irving; skip anything explicitly elsewhere.
    if address and not re.search(r'\bIrving\b', address, re.I):
        return None
    if not title or not event_date or not url or not venue:
        return None

    description = clean_text(event.get('description')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(event.get('startDate')) or parse_clock(event.get('_listed_time')),
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_clock(value):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip().lower(), '%I:%M %p').strftime('%H:%M')
    except ValueError:
        return None


def calendar_attributes(markup):
    matches = re.findall(r'atts:\s*"([^"]+)"', markup)
    if not matches:
        raise ValueError('MEC calendar attributes were not found')
    # The final initialization block is the list view and includes its page size.
    return parse_qsl(html.unescape(matches[-1]), keep_blank_values=True)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    response = session.get(CALENDAR_URL, timeout=45)
    response.raise_for_status()
    markup = response.text
    attributes = calendar_attributes(markup)
    soup = BeautifulSoup(markup, 'html.parser')
    container = soup.select_one('.mec-full-calendar-skin-container')
    if not container:
        raise ValueError('MEC calendar container was not found')
    calendar_markup = str(container)
    # MEC places the initial page's JSON-LD scripts adjacent to (rather than
    # inside) its calendar container, so retain the full document for page one.
    fragments = [markup]

    session.headers.update({
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Origin': SOURCE_URL.rstrip('/'),
        'Referer': CALENDAR_URL,
        'X-Requested-With': 'XMLHttpRequest',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
    })

    # Initial HTML contains the first page. MEC returns the cursor needed by the
    # following page; stop defensively if a cursor repeats.
    end_dates = re.findall(r'end_date:\s*"(\d{4}-\d{2}-\d{2})"', markup)
    offsets = re.findall(r'offset:\s*"(\d+)"', markup)
    start_date = end_dates[-1] if end_dates else None
    offset = int(offsets[-1]) if offsets else 0
    divider = start_date.replace('-', '')[:6] if start_date else ''
    seen_cursors = set()
    while True:
        if start_date is None:
            break

        cursor = (start_date, offset, divider)
        if cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
        data = list(attributes) + [
            ('action', 'mec_list_load_more'),
            ('mec_start_date', start_date),
            ('mec_offset', str(offset)),
            ('current_month_divider', divider),
            ('apply_sf_date', '0'),
        ]
        page = session.post(AJAX_URL, data=data, timeout=60)
        page.raise_for_status()
        payload = page.json()
        page_markup = payload.get('html') or ''
        if not page_markup:
            break
        fragments.append(page_markup)
        if not payload.get('has_more_event'):
            break
        start_date = payload.get('end_date')
        offset = payload.get('offset', 0)
        divider = payload.get('current_month_divider', '')

    records = []
    for fragment in fragments:
        for event in iter_event_json(fragment):
            record = make_record(event)
            if record:
                records.append(record)

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    result = sorted(
        unique.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )
    log_message(
        'Irving Arts Center calendar scraped',
        event='crawler_scrape_completed',
        record_count=len(result),
        url=CALENDAR_URL,
    )
    return result


class IrvingArtsCenterComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='irvingartscenter_com',
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
        return get_concerts()


def main():
    IrvingArtsCenterComCrawler().run()


if __name__ == '__main__':
    main()
