import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://alabamasymphony.org/'
EVENTS_URL = f'{SOURCE_URL}concert/'
SOURCE = 'Alabama Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUE_CITIES = {
    'alabama theatre': 'Birmingham',
    'alys stephens center': 'Birmingham',
    'avon theater': 'Decatur',
    'birmingham botanical gardens': 'Birmingham',
    "birmingham children's theatre": 'Birmingham',
    'bjcc concert hall': 'Birmingham',
    'hoover high school performing arts center': 'Hoover',
    'saturn birmingham': 'Birmingham',
    'thompson high school performing arts center': 'Alabaster',
}

DATE_TIME_RE = re.compile(
    r'(?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})\s+'
    r'(?P<time>\d{1,2}:\d{2}\s*[ap]m)',
    re.IGNORECASE,
)
RANGE_RE = re.compile(
    r'(?P<start>[A-Z][a-z]+ \d{1,2}, \d{4})\s+'
    r'(?P<time>\d{1,2}:\d{2}\s*[ap]m)\s*[–-]\s*'
    r'(?P<end>[A-Z][a-z]+ \d{1,2}, \d{4})',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def archive_items(session):
    soup = get_soup(session, EVENTS_URL)
    items = []
    for article in soup.select('main article'):
        link = article.select_one('h3 a[href]') or article.select_one('a[href]')
        title_node = article.select_one('h3')
        strongs = article.select('strong')
        if not link or not title_node or len(strongs) < 2:
            continue
        items.append(
            {
                'url': link.get('href', '').strip(),
                'title': clean_text(title_node),
                'venue': clean_text(strongs[0]),
                'when': clean_text(strongs[1]),
                'description': clean_text(article.select_one('p') or article),
            }
        )
    return items


def parse_when(value):
    occurrences = []
    range_match = RANGE_RE.search(value)
    if range_match:
        start = datetime.strptime(range_match.group('start'), '%B %d, %Y').date()
        end = datetime.strptime(range_match.group('end'), '%B %d, %Y').date()
        if end >= start and (end - start).days <= 14:
            # A displayed run does not prove that a performance occurs on
            # every intervening day. Preserve its explicit starting occurrence.
            occurrences.append((start.isoformat(), parse_time(range_match.group('time'))))
            value = value[range_match.end():]

    for match in DATE_TIME_RE.finditer(value):
        event_date = datetime.strptime(match.group('date'), '%B %d, %Y').date()
        occurrences.append((event_date.isoformat(), parse_time(match.group('time'))))
    return list(dict.fromkeys(occurrences))


def parse_time(value):
    return datetime.strptime(re.sub(r'\s+', ' ', value.strip().upper()), '%I:%M %p').strftime('%H:%M')


def city_for_venue(venue):
    normalized = venue.lower().replace('’', "'")
    for name, city in VENUE_CITIES.items():
        if name in normalized:
            return city
    return None


def detail_data(session, item):
    soup = get_soup(session, item['url'])
    title_node = soup.select_one('.event-left > h1') or soup.select_one('main h1')
    title = clean_text(title_node) or item['title']

    venue_node = soup.select_one('.event-right .where > strong') or soup.select_one('.where > strong')
    venue = clean_text(venue_node) or item['venue']

    when_nodes = soup.select('.event-right .when > strong') or soup.select('.when > strong')
    when_values = list(dict.fromkeys(clean_text(node) for node in when_nodes))
    occurrences = []
    for value in when_values or [item['when']]:
        occurrences.extend(parse_when(value))
    occurrences = list(dict.fromkeys(occurrences))

    description_parts = []
    for selector in ('.description', '.artists', '.program_to_include'):
        text = clean_text(soup.select_one(selector))
        if text and text not in description_parts:
            description_parts.append(text)
    if not description_parts:
        page_body = clean_text(soup.select_one('main article'))
        if page_body:
            description_parts.append(page_body)
    description = clean_text('\n\n'.join(description_parts)) or item['description'] or None
    return title, venue, occurrences, description


def make_records(session, item):
    try:
        title, venue, occurrences, description = detail_data(session, item)
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Failed to scrape concert detail',
            event='crawler_item_failed',
            level='warning',
            url=item['url'],
            error_type=type(error).__name__,
            error_message=str(error),
        )
        title = item['title']
        venue = item['venue']
        occurrences = parse_when(item['when'])
        description = item['description'] or None

    city = city_for_venue(venue)
    if not title or not item['url'] or not venue or not city:
        return []
    return [
        {
            'title': title,
            'date': event_date,
            'url': item['url'],
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from in occurrences
    ]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = archive_items(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(make_records, session, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to process concert',
                    event='crawler_item_failed',
                    level='warning',
                    url=item['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title'], record['url']),
    )


class AlabamaSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='alabamasymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
    AlabamaSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
