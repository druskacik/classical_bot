import json
import re
from datetime import date, timedelta
from html import unescape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.orchestre-orleans.com/'
SOURCE = "Orchestre Symphonique d'Orléans"
EVENTS_API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/event_listing')
HEADERS = {
    'Accept': 'application/json, text/html;q=0.9,*/*;q=0.8',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_text(value):
    if not value:
        return ''
    raw = unescape(str(value))
    text = (
        BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
        if '<' in raw
        else raw.strip()
    )
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_iso_date(value):
    try:
        return date.fromisoformat(clean_text(value)[:10])
    except (TypeError, ValueError):
        return None


def event_schema(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return {}


def location_fields(location):
    if not isinstance(location, dict):
        return '', ''
    venue = clean_text(location.get('name'))
    address = location.get('address') or ''
    if isinstance(address, dict):
        city = clean_text(address.get('addressLocality'))
    else:
        city = ''
        address = clean_text(address)

    evidence = f'{venue} {address}'
    if not city and re.search(r"\borl[eé]ans\b", evidence, re.I):
        city = 'Orléans'
    if not venue or venue.casefold() == city.casefold():
        venue = ''
    return venue, city


def detail_records(session, event):
    url = clean_text(event.get('link'))
    title = clean_text((event.get('title') or {}).get('rendered'))
    if not url or not title:
        return []

    response = session.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    schema = event_schema(soup)
    start = parse_iso_date(schema.get('startDate'))
    end = parse_iso_date(schema.get('endDate')) or start
    venue, city = location_fields(schema.get('Location') or schema.get('location'))

    if not all((start, end, venue, city)) or end < start or (end - start).days > 14:
        log_message(
            'Skipped incomplete Orchestre Symphonique d’Orléans event',
            event='crawler_item_skipped',
            level='warning',
            url=url,
            error_type='IncompleteEventData',
            error_message='Required date, venue, or city is missing or date range is invalid',
        )
        return []

    descriptions = []
    for value in (
        schema.get('description'),
        (event.get('content') or {}).get('rendered'),
    ):
        text = clean_text(value)
        if text and text not in descriptions:
            descriptions.append(text)

    # The plugin models consecutive performances as an inclusive start/end
    # range. Emit each advertised calendar date as its own occurrence.
    records = []
    current = start
    while current <= end:
        records.append({
            'title': title,
            'date': current.isoformat(),
            'url': url,
            # Current pages contain CMS modification times in schema markup,
            # not advertised curtain times, so deliberately leave time empty.
            'time_from': None,
            'venue': venue,
            'city': city,
            'country_code': 'FR',
            'description': '\n\n'.join(descriptions) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
        current += timedelta(days=1)
    return records


class OrchestreOrleansComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestre_orleans_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        records = []
        page = 1
        while True:
            response = session.get(
                EVENTS_API_URL,
                params={'per_page': 100, 'page': page, 'orderby': 'id', 'order': 'asc'},
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            events = response.json()
            for event in events:
                records.extend(detail_records(session, event))

            total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            if page >= total_pages:
                break
            page += 1

        return sorted(records, key=lambda item: (item['date'], item['title'], item['venue']))


def main():
    OrchestreOrleansComCrawler().run()


if __name__ == '__main__':
    main()
