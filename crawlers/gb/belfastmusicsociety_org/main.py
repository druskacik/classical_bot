import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.belfastmusicsociety.org/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/product'
EVENTS_URL = f'{SOURCE_URL}events/'
SOURCE = 'Belfast Music Society'
CITY = 'Belfast'

# Every first-party event category exposed by the site's product API.  The
# unclassified shop category and the donation category are deliberately absent.
EVENT_CATEGORY_IDS = (52, 53, 56, 57, 58, 59, 60, 61, 62, 63, 64, 76)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def product_items(session):
    """Return API records for the concrete events in the live events feed.

    Expired products remain in the API, but the site removes their occurrence
    metadata.  They cannot safely become concert rows once their date and venue
    are gone, so discovery starts from the site's current Events page.
    """
    soup = BeautifulSoup(get_response(session, EVENTS_URL).content, 'html.parser')
    links = []
    for node in soup.select('a[href*="/product/"]'):
        url = node.get('href', '').split('#', 1)[0]
        if url and url not in links:
            links.append(url)

    items = []
    for url in links:
        slug = urlparse(url).path.rstrip('/').rsplit('/', 1)[-1]
        response = get_response(session, API_URL, params={'slug': slug, 'per_page': 1})
        matches = response.json()
        if matches and set(matches[0].get('product_cat', ())) & set(EVENT_CATEGORY_IDS):
            items.append(matches[0])
    return items


def labelled_value(soup, label):
    for container in soup.select('.media-body'):
        label_node = container.select_one('.sub-lb')
        if clean_text(label_node).casefold() != label.casefold():
            continue
        value_node = container.select_one('.media-heading')
        return clean_text(value_node)
    return ''


def parse_date(value):
    try:
        return datetime.strptime(value, '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b', value, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3).casefold() == 'pm' and hour != 12:
        hour += 12
    elif match.group(3).casefold() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def parse_product(item, content):
    soup = BeautifulSoup(content, 'html.parser')
    event_date = parse_date(labelled_value(soup, 'Details'))
    address = labelled_value(soup, 'Address')
    title = clean_text(item.get('title', {}).get('rendered'))
    if not title or not event_date or not address or 'belfast' not in address.casefold():
        return None

    venue = clean_text(address.split(',', 1)[0])
    if not venue or venue.casefold() == CITY.casefold():
        return None

    description_soup = BeautifulSoup(item.get('content', {}).get('rendered', ''), 'html.parser')
    description = clean_text(description_soup) or None
    return {
        'title': title,
        'date': event_date,
        'url': item['link'],
        'time_from': parse_time(labelled_value(soup, 'Time')),
        'venue': venue,
        'city': CITY,
        'country_code': 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = product_items(session)
    records = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(get_response, session, item['link']): item
            for item in items
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                record = parse_product(item, future.result().content)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Belfast Music Society event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=item['link'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class BelfastMusicSocietyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='belfastmusicsociety_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
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
    BelfastMusicSocietyOrgCrawler().run()


if __name__ == '__main__':
    main()
