import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.chaise-dieu.com/'
SOURCE = 'Festival de La Chaise-Dieu'
EVENTS_API = f'{SOURCE_URL}wp-json/wp/v2/programmation'

# French-language performance categories.  Conference and musical-discussion
# terms are intentionally excluded because they are not concert occurrences.
PERFORMANCE_CATEGORY_IDS = (39, 416, 53)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def listing_events(session):
    events = []
    page = 1
    while True:
        response = session.get(
            EVENTS_API,
            params={
                'per_page': 100,
                'page': page,
                'status': 'publish',
                'categorie_evenements': ','.join(map(str, PERFORMANCE_CATEGORY_IDS)),
                '_fields': 'id,link,title,categorie_evenements',
            },
            timeout=60,
        )
        response.raise_for_status()
        events.extend(response.json())
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            return events
        page += 1


def parse_date(soup):
    node = soup.select_one('.single-event__date')
    if not node:
        return None
    numbers = re.findall(r'\d+', clean_text(node))
    if len(numbers) < 3:
        return None
    try:
        return date(int(numbers[-1]), int(numbers[-2]), int(numbers[-3])).isoformat()
    except ValueError:
        return None


def parse_hour_place(soup):
    text = clean_text(soup.select_one('.single-event__hourplace'))
    if not text:
        return None

    time_match = re.match(r'\s*([01]?\d|2[0-3])\s*[hH](?:\s*([0-5]\d))?\b', text)
    time_from = None
    if time_match:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2) or "00"}'
        text = text[time_match.end():].strip(' ,-')

    venue_city = re.match(r'(.+?)(?:,|\s+-\s+)\s*([^,]+)$', text)
    if not venue_city:
        return None
    venue = clean_text(venue_city.group(1))
    city = clean_text(venue_city.group(2))
    if not venue or not city:
        return None
    return time_from, venue, city


def detail_record(session, event):
    url = event.get('link') or ''
    if not url:
        return None
    response = session.get(url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')

    title = clean_text(soup.select_one('main article h2.single-event__title'))
    event_date = parse_date(soup)
    hour_place = parse_hour_place(soup)
    if not title or not event_date or not hour_place:
        return None

    # The first tab is the editorial description, distribution, and full
    # programme.  Later tabs contain prices and are deliberately ignored.
    description_node = soup.select_one('.single-event__accordeons .tabcontent')
    description = clean_text(description_node) or None
    time_from, venue, city = hour_place
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'FR',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        events = listing_events(session)
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Failed to fetch Festival de La Chaise-Dieu catalogue',
            event='crawler_fetch_failed',
            level='error',
            url=EVENTS_API,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    records = []
    for event in events:
        url = event.get('link') or EVENTS_API
        try:
            record = detail_record(session, event)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Festival de La Chaise-Dieu event',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class ChaiseDieuComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chaise_dieu_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    ChaiseDieuComCrawler().run()


if __name__ == '__main__':
    main()
