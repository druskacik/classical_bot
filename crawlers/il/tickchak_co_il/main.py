import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://tickchak.co.il/'
SOURCE = "Tickchak"
LIVE_URL = 'https://live.tickchak.co.il/'

# Tickchak is a mixed ticketing platform. These are its first-party categories
# which can contain events accepted by the project, but none is a reliable
# classical-only filter, so their combined output is sent for classification.
CATEGORY_PATHS = (
    'shows',              # category id 1: live shows
    'childrens-shows',    # category id 24: children's shows
    'theater',            # category id 10: theater (may include opera/dance)
    'musical',            # category id 27: musicals
    'cantorial-music',    # category id 25: cantorial music
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'he-IL,he;q=0.9,en;q=0.7',
}

CITY_ALIASES = {
    'אודטוריום חיפה': 'חיפה',
    'בית שמואל': 'ירושלים',
    'בנייני האומה': 'ירושלים',
    'בנייני האומה- אולם אוסשקין': 'ירושלים',
    'בריכת הסולטן': 'ירושלים',
    'בריכת הסולטן ירושלים': 'ירושלים',
    'היכל התרבות ירושלים': 'ירושלים',
    'היכל התרבות נתניה': 'נתניה',
    'המשכן לאמנויות הבמה ב״ש': 'באר שבע',
    'מרכז הכנסים אשקלון': 'אשקלון',
    'תיאטרון הירש בית שמואל': 'ירושלים',
    'תיאטרון ירושלים שרובר': 'ירושלים',
}

VALID_CITIES = {
    'אשקלון', 'באר שבע', 'בית אל', 'גן שמואל', 'חיפה', 'ירושלים',
    'מגדל', 'מודיעין מכבים רעות', 'נתניה', 'עכו', 'פרדסיה', 'פתח תקווה',
    'צפת', 'קרית טבעון', 'ראשון לציון', 'רמת השרון', 'תל אביב',
}


def clean_text(value):
    if value is None:
        return ''
    return ' '.join(str(value).replace('\xa0', ' ').split()).strip()


def canonical_url(value):
    value = clean_text(value).removesuffix('#event')
    if not value:
        return ''
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))


def event_nodes(html):
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, list):
            nodes = payload
        elif isinstance(payload, dict):
            nodes = payload.get('@graph', [payload])
        else:
            nodes = []
        for node in nodes:
            node_types = node.get('@type', []) if isinstance(node, dict) else []
            if isinstance(node_types, str):
                node_types = [node_types]
            if 'Event' in node_types:
                yield node


def listing_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        nodes = payload.get('@graph', []) if isinstance(payload, dict) else []
        for node in nodes:
            if not isinstance(node, dict) or node.get('@type') != 'CollectionPage':
                continue
            item_list = node.get('mainEntity') or {}
            for entry in item_list.get('itemListElement') or []:
                item = entry.get('item') or {}
                url = canonical_url(item.get('@id') if isinstance(item, dict) else item)
                if url:
                    urls.append(url)
    return list(dict.fromkeys(urls))


def parse_event(node):
    title = clean_text(node.get('name'))
    start = clean_text(node.get('startDate'))
    location = node.get('location') or {}
    address = location.get('address') or {}
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    city = CITY_ALIASES.get(city, city)
    offers = node.get('offers') or {}
    if isinstance(offers, list):
        offers = next((offer for offer in offers if isinstance(offer, dict)), {})
    url = canonical_url(offers.get('url') or node.get('url') or node.get('@id'))

    try:
        event_date = date.fromisoformat(start[:10]).isoformat()
    except (TypeError, ValueError):
        return None
    time_from = start[11:16] if len(start) >= 16 and start[10] == 'T' else None
    if not title or not url or not venue or city not in VALID_CITIES or venue == city:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'IL',
        'description': clean_text(node.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class TickchakCoIlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tickchak_co_il',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IL',
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
        records = []
        detail_urls = []
        for path in CATEGORY_PATHS:
            url = f'{LIVE_URL}{path}'
            try:
                response = session.get(url, timeout=60)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Tickchak category',
                    event='crawler_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            for node in event_nodes(response.text):
                record = parse_event(node)
                if record:
                    records.append(record)
            detail_urls.extend(listing_urls(response.text))

        # Category pages publish their complete, date-ordered catalogue as a
        # Schema.org ItemList. Detail pages contain the concrete Event nodes,
        # including all separately dated performances and fuller descriptions.
        def fetch_detail(url):
            response = session.get(url, timeout=60)
            response.raise_for_status()
            return [record for record in (
                parse_event(node) for node in event_nodes(response.text)
            ) if record]

        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {
                executor.submit(fetch_detail, url): url
                for url in dict.fromkeys(detail_urls)
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Tickchak event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        unique = {
            (record['title'], record['date'], record['time_from'],
             record['venue'], record['city']): record
            for record in records
        }
        return sorted(
            unique.values(),
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    TickchakCoIlCrawler().run()


if __name__ == '__main__':
    main()
