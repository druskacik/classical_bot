import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hfmdd.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'besuchen')
EVENTS_API = SOURCE_URL
SOURCE = 'Hochschule für Musik Carl Maria von Weber Dresden'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9',
}

API_PARAMS = {
    'type': '1341',
    'tx_dtmevents_list[action]': 'getDtmEventsMonths',
    'tx_dtmevents_list[controller]': 'Event',
    'L': '0',
}

# The calendar includes the university's lectures, workshops, and Jazz/Rock/Pop
# events alongside classical performances, and its monthly endpoint has no
# public category constraint. Keep the full candidate feed for classification.


def clean_text(node):
    if node is None:
        return ''
    value = node.get_text(' ', strip=True) if hasattr(node, 'get_text') else str(node)
    return re.sub(r'\s+', ' ', value.replace('\xa0', ' ')).strip()


def parse_month(html):
    soup = BeautifulSoup(html, 'html.parser')
    month_node = soup.select_one('#dtmevents_months_current')
    month = month_node.get('value') if month_node else None
    events = []
    for item in soup.select('article.dtmevents_item'):
        time_node = item.select_one('time[datetime]')
        title_link = item.select_one('a.event-title[href]')
        venue_node = item.select_one('.col-md-6 small')
        if not time_node or not title_link or not venue_node:
            continue
        try:
            occurrence = datetime.fromisoformat(time_node['datetime'])
        except (ValueError, TypeError):
            continue
        venue = clean_text(venue_node)
        if not venue:
            continue
        events.append({
            'date': occurrence.date().isoformat(),
            'time_from': occurrence.strftime('%H:%M'),
            'url': urljoin(CALENDAR_URL, title_link['href']),
            'listing_title': clean_text(title_link).removesuffix(' »').strip(),
            'listing_venue': venue,
        })
    return month, events


def parse_location(text, venue):
    location = clean_text(text)
    venue = clean_text(venue)
    postcode_match = re.search(r'\b\d{5}\s+([^,;]+)', location)
    if postcode_match:
        city = postcode_match.group(1).strip(' .')
        if city:
            return venue, city

    # These are first-party names for the Dresden campus and its two halls.
    if any(marker in venue.casefold() for marker in (
        'hochschule für musik dresden', 'wettiner platz', 'kleiner saal',
        'konzertsaal', 'probebühne',
    )):
        return venue, 'Dresden'
    return None, None


def parse_event_page(html, occurrence):
    soup = BeautifulSoup(html, 'html.parser')
    root = soup.select_one('.tx-dtmevents')
    if root is None:
        return None
    title = clean_text(root.select_one('.d-none.d-lg-block h1') or root.select_one('h1'))
    info = root.select_one('.NO_col-sm-6_col-md-3 p')
    info_text = clean_text(info)
    date_match = re.search(r'\b(\d{2}\.\d{2}\.\d{2})\b', info_text)
    time_match = re.search(r'\b(\d{1,2}:\d{2})\b', info_text)
    if not date_match:
        return None
    try:
        event_date = datetime.strptime(date_match.group(1), '%d.%m.%y').date().isoformat()
    except ValueError:
        return None
    if event_date != occurrence['date']:
        return None

    strong = info.select_one('strong') if info else None
    location_text = info_text.replace(clean_text(strong), '', 1) if strong else info_text
    venue, city = parse_location(location_text, occurrence['listing_venue'])
    description_nodes = root.select('.col-md-12.pb-2')
    description = '\n\n'.join(filter(None, (clean_text(node) for node in description_nodes))) or None
    if not all((title, event_date, occurrence['url'], venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': occurrence['url'],
        'time_from': time_match.group(1) if time_match else occurrence['time_from'],
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class HfmddDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hfmdd_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date'],
    )

    def _move_month(self, session, current, arrow):
        response = session.post(
            EVENTS_API,
            params=API_PARAMS,
            data={
                'tx_dtmevents_list[arrow]': arrow,
                'tx_dtmevents_list[current]': current,
                'tx_dtmevents_list[eventtypes]': '',
            },
            headers={'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'},
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get('action') != 'OK':
            return None, [], True
        month, events = parse_month(payload.get('content', ''))
        return month, events, bool(payload.get('noback'))

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(CALENDAR_URL, timeout=45)
        response.raise_for_status()
        current, initial_events = parse_month(response.text)
        if not current:
            raise ValueError('Calendar did not expose its current month')

        occurrences = {(item['url'], item['date']): item for item in initial_events}

        cursor = current
        empty_months = 0
        for _ in range(240):
            month, events, no_more = self._move_month(session, cursor, 'back')
            if not month or month == cursor:
                break
            for item in events:
                occurrences[(item['url'], item['date'])] = item
            empty_months = 0 if events else empty_months + 1
            cursor = month
            # The endpoint never sets noback after its archive ends. Two years
            # of consecutive empty monthly responses is therefore its effective
            # archive boundary (the last published occurrence is retained).
            if no_more or empty_months >= 24:
                break

        cursor = current
        empty_months = 0
        for _ in range(60):
            month, events, _ = self._move_month(session, cursor, 'next')
            if not month or month == cursor:
                break
            for item in events:
                occurrences[(item['url'], item['date'])] = item
            empty_months = 0 if events else empty_months + 1
            cursor = month
            if empty_months >= 12:
                break

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(session.get, item['url'], timeout=45): item
                for item in occurrences.values()
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    detail = future.result()
                    detail.raise_for_status()
                    record = parse_event_page(detail.text, item)
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch HfM Dresden event detail',
                        event='crawler_fetch_failed', level='warning', url=item['url'],
                        error_type=type(error).__name__, error_message=str(error),
                    )

        records.sort(key=lambda row: (row['date'], row['time_from'] or '', row['title']))
        return records


def main():
    HfmddDeCrawler().run()


if __name__ == '__main__':
    main()
