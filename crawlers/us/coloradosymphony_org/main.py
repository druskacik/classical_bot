import re
from datetime import date
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://coloradosymphony.org/'
SOURCE = 'Colorado Symphony'
CALENDAR_URL = SOURCE_URL + 'calendar/{year}/{month}/'
EVENTS_API_URL = SOURCE_URL + 'wp/wp-admin/admin-post.php'
MONTH_NAMES = (
    'january', 'february', 'march', 'april', 'may', 'june',
    'july', 'august', 'september', 'october', 'november', 'december',
)
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
    return re.sub(r'\s+', ' ', value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)).strip()


def month_offset(year, month, offset):
    index = year * 12 + month - 1 + offset
    return index // 12, index % 12 + 1


def venue_and_city(title, url):
    normalized = title.casefold()
    named_venues = (
        ('studio loft', 'The Studio Loft at Ellie Caulkins Opera House', 'Denver'),
        ('arvada center', 'Arvada Center for the Arts and Humanities', 'Arvada'),
        ('red rocks', 'Red Rocks Amphitheatre', 'Morrison'),
        ('boettcher concert hall', 'Boettcher Concert Hall', 'Denver'),
    )
    for marker, venue, city in named_venues:
        if marker in normalized:
            return venue, city

    # The orchestra's own Tessitura inventory is overwhelmingly its home-hall
    # programme. Externally ticketed dates are often tours or summer concerts,
    # so they are skipped unless the title identifies a known venue above.
    if urlparse(url).hostname == 'tickets.coloradosymphony.org':
        return 'Boettcher Concert Hall', 'Denver'
    return None, None


def parse_month(html, year, month):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for day_node in soup.select('li.list-item-day.active-date[id]'):
        try:
            day = int(day_node['id'])
            event_date = date(year, month, day).isoformat()
        except (KeyError, TypeError, ValueError):
            continue

        events = day_node.select('.mot-calendar-event')
        if not events:
            events = day_node.select('div.calendar-event')
        for event in events:
            link = event.select_one('.event-name a[href], .mot-event-name a[href]')
            time_node = event.select_one('.event-time')
            title = clean_text(link)
            url = link.get('href', '').strip() if link else ''
            if not title or not url:
                continue
            venue, city = venue_and_city(title, url)
            if not venue or not city:
                continue
            time_text = clean_text(time_node)
            match = re.search(r'(?i)\b(\d{1,2}):(\d{2})\s*([AP]M)\b', time_text)
            time_from = None
            if match:
                hour = int(match.group(1)) % 12 + (12 if match.group(3).upper() == 'PM' else 0)
                time_from = f'{hour:02d}:{int(match.group(2)):02d}'
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'description': None,
            })
    return records


def fetch_descriptions(session):
    descriptions = {}
    start_at = 0
    while True:
        response = session.post(
            EVENTS_API_URL,
            data={'category': '', 'start_at': start_at, 'action': 'fetch_events_ajax'},
            headers={**HEADERS, 'Referer': SOURCE_URL + 'view-all-events/'},
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get('status') != 'success':
            raise ValueError('Colorado Symphony events endpoint returned an unexpected response')
        soup = BeautifulSoup(payload.get('event_html') or '', 'html.parser')
        cards = soup.select('.production-event')
        for card in cards:
            description = clean_text(card.select_one('.editor-content')) or None
            for link in card.select('a.performance-link[href]'):
                descriptions[link['href'].strip().rstrip('/')] = description
        if not payload.get('has_more_events') or not cards:
            break
        start_at += len(cards)
    return descriptions


class ColoradosymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='coloradosymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def __init__(self, month_range=None):
        self.month_range = month_range

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        today = date.today()

        records = []
        if self.month_range is not None:
            months = list(self.month_range)
        else:
            months = [month_offset(today.year, today.month, -offset) for offset in range(240)]

        for index, (year, month) in enumerate(months):
            url = CALENDAR_URL.format(year=year, month=MONTH_NAMES[month - 1])
            try:
                response = session.get(url, timeout=60)
                response.raise_for_status()
                month_records = parse_month(response.text, year, month)
            except requests.RequestException as error:
                log_message(
                    'Colorado Symphony calendar request failed',
                    event='crawler_request_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            records.extend(month_records)
            if self.month_range is None and index >= 11:
                recent_months = months[index - 11:index + 1]
                recent_keys = {(item['date'][:7]) for item in records}
                if all(f'{y:04d}-{m:02d}' not in recent_keys for y, m in recent_months):
                    break

        if self.month_range is None:
            for forward in range(1, 25):
                year, month = month_offset(today.year, today.month, forward)
                url = CALENDAR_URL.format(year=year, month=MONTH_NAMES[month - 1])
                try:
                    response = session.get(url, timeout=60)
                    response.raise_for_status()
                    records.extend(parse_month(response.text, year, month))
                except requests.RequestException as error:
                    log_message(
                        'Colorado Symphony calendar request failed',
                        event='crawler_request_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        try:
            descriptions = fetch_descriptions(session)
        except (requests.RequestException, ValueError) as error:
            descriptions = {}
            log_message(
                'Colorado Symphony programme descriptions unavailable',
                event='crawler_detail_enrichment_failed',
                level='warning',
                url=EVENTS_API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )

        for record in records:
            record['description'] = descriptions.get(record['url'].rstrip('/'))

        records.sort(key=lambda item: (item['date'], item['time_from'] or '', item['title']))
        if not records:
            log_message(
                'No Colorado Symphony performances found',
                event='crawler_empty_listing',
                level='warning',
                url=SOURCE_URL,
                record_count=0,
            )
        return records


def main():
    ColoradosymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
