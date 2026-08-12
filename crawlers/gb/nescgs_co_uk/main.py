from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.nescgs.co.uk/'
EVENTS_URL = urljoin(SOURCE_URL, 'ConcertsAndWorkshops')
SOURCE = 'North East Scotland Classical Guitar Society'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

# The archive has no structured location field. These are the named venues used
# on its detail pages; ordering puts specific rooms before their parent venue.
VENUES = (
    (r'Phoenix Community Centre', 'Phoenix Community Centre', 'Aberdeen'),
    (r'Aberdeen(?:\u2019|\'|’)?s? Northern Hotel|Aberdeen Northern Hotel',
     'Aberdeen Northern Hotel', 'Aberdeen'),
    (r'Queen(?:\u2019|\'|’)?s Cross Church|Queens Cross Church',
     "Queen's Cross Church", 'Aberdeen'),
    (r'Woodend Barn', 'Woodend Barn', 'Banchory'),
    (r'(?:The )?Acorn Centre', 'The Acorn Centre', 'Inverurie'),
    (r'Kemnay Church (?:Centre|Hall)', 'Kemnay Church Centre', 'Kemnay'),
    (r'Pitmedden Garden Estate', 'Pitmedden Garden Estate', 'Pitmedden'),
    (r'(?:the )?Implements Shed', 'The Implements Shed', 'Pitmedden'),
)


def clean_text(node):
    if not node:
        return ''
    text = node.get_text('\n', strip=True) if hasattr(node, 'get_text') else str(node)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def parse_index(content):
    soup = BeautifulSoup(content, 'html.parser')
    events = []
    for row in soup.select('table.views-table tbody tr'):
        link = row.select_one('td.views-field-title a[href]')
        date_node = row.select_one('.date-display-single[content]')
        if not link or not date_node:
            continue
        raw_date = date_node.get('content', '')[:10]
        try:
            event_date = datetime.strptime(raw_date, '%Y-%m-%d').date().isoformat()
        except ValueError:
            continue
        raw_datetime = date_node.get('content', '')
        time_match = re.search(r'T(\d{2}:\d{2})', raw_datetime)
        events.append({
            'title': clean_text(link),
            'date': event_date,
            'time_from': time_match.group(1) if time_match else None,
            'url': urljoin(SOURCE_URL, link['href']),
        })
    return events


def resolve_location(text):
    for pattern, venue, city in VENUES:
        if re.search(pattern, text, re.IGNORECASE):
            return venue, city
    return None, None


def parse_detail(content, event):
    soup = BeautifulSoup(content, 'html.parser')
    body = soup.select_one('.node .field-name-body')
    description = clean_text(body)
    venue, city = resolve_location(description)
    if not description or not venue or not city:
        return None
    return {
        **event,
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class NescgsCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nescgs_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        events = parse_index(get_response(session, EVENTS_URL).content)
        records = []

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(get_response, session, event['url']): event
                for event in events
            }
            for future in as_completed(futures):
                event = futures[future]
                try:
                    record = parse_detail(future.result().content, event)
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape NESCGS event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=event['url'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    NescgsCoUkCrawler().run()


if __name__ == '__main__':
    main()
