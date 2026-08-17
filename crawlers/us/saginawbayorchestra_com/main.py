import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://saginawbayorchestra.com/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/mec-events'
SOURCE = 'Saginaw Bay Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    raw = html.unescape(str(value))
    text = (
        BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
        if '<' in raw
        else raw.strip()
    )
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    value = clean_text(value)
    for pattern in ('%b %d %Y', '%B %d %Y'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def parse_time(value):
    match = re.search(r'(?i)\b(\d{1,2}):?(\d{2})?\s*([ap]m)\b', clean_text(value))
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'pm':
        hour += 12
    return f'{hour:02d}:{match.group(2) or "00"}'


def parse_city(address):
    address = clean_text(address)
    # Published locations consistently use Michigan postal addresses, either
    # "Saginaw MI 48607" or "Bay City, MI" style.
    match = re.search(
        r'([A-Za-z][A-Za-z .\'-]*?)\s*,?\s+MI(?:\s+\d{5}(?:-\d{4})?)?\s*$',
        address,
        re.I,
    )
    if not match:
        return ''
    candidate = match.group(1).strip(' ,')
    # Without a comma the match can include the street. Prefer the known
    # locality at the end, while leaving comma-delimited new cities generic.
    for city in ('Bay City', 'Saginaw'):
        if re.search(rf'\b{re.escape(city)}$', candidate, re.I):
            return city
    return candidate.split(',')[-1].strip()


def parse_event(page_html, url):
    soup = BeautifulSoup(page_html, 'html.parser')
    title = clean_text(soup.select_one('.mec-single-title'))
    event_date = parse_date(soup.select_one('.mec-start-date-label'))
    time_from = parse_time(soup.select_one('.mec-single-event-time abbr'))
    venue = clean_text(soup.select_one('.mec-single-event-location .author'))
    address = clean_text(soup.select_one('.mec-single-event-location .mec-address'))
    city = parse_city(address)
    description = clean_text(soup.select_one('.mec-single-event-description')) or None
    if not title or not event_date or not venue or not city:
        return None
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


def fetch_event(session, event):
    url = clean_text(event.get('link'))
    if not url:
        return None
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return parse_event(response.text, url)


def list_events(session):
    events = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page, 'orderby': 'date', 'order': 'desc'},
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        events.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return events
        page += 1


class SaginawBayOrchestraComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='saginawbayorchestra_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        events = list_events(session)
        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch_event, session, event): event for event in events}
            for future in as_completed(futures):
                event = futures[future]
                url = clean_text(event.get('link'))
                try:
                    record = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape Saginaw Bay event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Saginaw Bay event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, venue, or city is missing',
                    )
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    SaginawBayOrchestraComCrawler().run()


if __name__ == '__main__':
    main()
