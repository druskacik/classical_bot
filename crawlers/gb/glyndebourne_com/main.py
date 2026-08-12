import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.glyndebourne.com/'
CALENDAR_URL = (
    'https://www.glyndebourne.com/wp-content/themes/'
    'glyndebourne/ajax/events-calendar.php'
)
SOURCE = 'Glyndebourne'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
    'X-Requested-With': 'XMLHttpRequest',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = str(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def fetch_calendar(session):
    response = session.get(CALENDAR_URL, timeout=60)
    response.raise_for_status()
    return response.json()


def event_schema(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        nodes = data.get('@graph', []) if isinstance(data, dict) else []
        for node in nodes:
            if node.get('@type') == 'Event':
                return node
    return {}


def detail_fields(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    schema = event_schema(soup)
    location = schema.get('location') or {}
    address = location.get('address') or {}
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    country = clean_text(address.get('addressCountry')).upper()

    parts = []
    summary = soup.select_one('.heroBanner__hero__heading-2')
    if clean_text(summary):
        parts.append(clean_text(summary))
    for block in soup.select('.block__text'):
        text = clean_text(block)
        if not text or text in parts or text == 'TICKET PRICES':
            continue
        # Ticket prices add noise but surrounding programme, cast, and timing
        # blocks are valuable input for later programme extraction.
        if re.search(r'(?im)^(?:stalls|foyer circle|circle|upper circle):\s*£', text):
            continue
        parts.append(text)
    return venue, city, country, '\n\n'.join(parts) or None


def calendar_records(calendar):
    records = []
    for day, events in calendar.items():
        if day == '1970-01-01':
            continue
        try:
            datetime.strptime(day, '%Y-%m-%d')
        except ValueError:
            continue
        for event in events if isinstance(events, list) else []:
            title = clean_text(event.get('title'))
            url = clean_text(event.get('permalink'))
            value = clean_text(event.get('date'))
            try:
                occurrence = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                continue
            if not title or not url or occurrence.date().isoformat() != day:
                continue
            records.append({
                'title': title,
                'date': day,
                'url': url,
                'time_from': occurrence.strftime('%H:%M'),
            })
    return records


class GlyndebourneComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='glyndebourne_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = calendar_records(fetch_calendar(session))
        details = {}
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(detail_fields, session, url): url
                for url in {record['url'] for record in records}
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    details[url] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Glyndebourne event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        complete = []
        for record in records:
            venue, city, country, description = details.get(
                record['url'], ('', '', '', None)
            )
            if not venue or not city or country != 'GB':
                continue
            record.update({
                'venue': venue,
                'city': city,
                'country_code': country,
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
            complete.append(record)
        return sorted(
            complete,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['url']
            ),
        )


def main():
    GlyndebourneComCrawler().run()


if __name__ == '__main__':
    main()
