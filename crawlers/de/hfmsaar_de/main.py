import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hfmsaar.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'veranstaltungskalender')
SOURCE = 'HfM Saar'

GERMAN_MONTHS = {
    'Jan': 1, 'Feb': 2, 'Mär': 3, 'Apr': 4, 'Mai': 5, 'Jun': 6,
    'Jul': 7, 'Aug': 8, 'Sep': 9, 'Okt': 10, 'Nov': 11, 'Dez': 12,
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def resolve_city(location):
    """Resolve the few touring venues separately from the Saarbrücken default."""
    normalized = clean_text(location).lower()
    explicit_cities = {
        r'\bhomburg\b': 'Homburg',
        r'\btrier\b': 'Trier',
        r'\billingen\b': 'Illingen',
        r'\bneunkirchen\b': 'Neunkirchen',
        r'\bsaarlouis\b': 'Saarlouis',
        r'\bst\. ingbert\b': 'St. Ingbert',
        r'\bst\. wendel\b': 'St. Wendel',
        r'\bvölklingen\b': 'Völklingen',
    }
    for pattern, city in explicit_cities.items():
        if re.search(pattern, normalized):
            return city

    # Statio Dominus Mundi is in the Illingen district of Wustweiler.
    if 'statio dominus mundi' in normalized:
        return 'Illingen'

    # The HfM calendar is based in Saarbrücken and its unnamed/local halls are
    # all there. Explicit touring cities above always take precedence.
    return 'Saarbrücken'


def listing_items(session):
    soup = fetch_soup(session, CALENDAR_URL)
    items = []
    for event in soup.select('.event.layout_list'):
        link = event.select_one('a[href*="veranstaltung-details/"]')
        date_tag = event.select_one('.date')
        time_tag = event.select_one('.time')
        location_tag = event.select_one('.location')
        if not link or not date_tag or not location_tag:
            continue

        title = clean_text(link)
        venue = re.sub(r'^Ort:\s*', '', clean_text(location_tag), flags=re.IGNORECASE)
        url = urljoin(CALENDAR_URL, link.get('href', ''))
        date_match = re.search(r'(\d{1,2})\s+([A-ZÄÖÜ][a-zäöü]{2})\s+(\d{4})', clean_text(date_tag))
        if not date_match or date_match.group(2) not in GERMAN_MONTHS:
            continue
        try:
            start = datetime(
                int(date_match.group(3)),
                GERMAN_MONTHS[date_match.group(2)],
                int(date_match.group(1)),
            )
        except ValueError:
            continue
        time_match = re.search(r'\b(\d{1,2}):(\d{2})\b', clean_text(time_tag))
        if not title or not venue or not url:
            continue
        items.append({
            'title': title,
            'date': start.date().isoformat(),
            'url': url,
            'time_from': f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None,
            'venue': venue,
            'city': resolve_city(venue),
        })
    return items


def detail_description(session, url):
    soup = fetch_soup(session, url)
    event = soup.select_one('.mod_eventreader .event')
    if not event:
        return None
    body = event.select_one('.column.col_8 .ce_text')
    return clean_text(body) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = listing_items(session)
    records = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(detail_description, session, item['url']): item
            for item in items
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                description = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=item['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                description = None
            records.append({
                **item,
                'country_code': 'DE',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class HfmsaarDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hfmsaar_de',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    HfmsaarDeCrawler().run()


if __name__ == '__main__':
    main()
