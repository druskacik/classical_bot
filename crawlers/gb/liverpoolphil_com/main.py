import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.liverpoolphil.com/'
SEARCH_URL = urljoin(SOURCE_URL, 'searchevent')
SOURCE = 'Liverpool Philharmonic'
CITY = 'Liverpool'
EVENT_CATEGORIES = (
    'Classical Music',
    'Comedy $ Spoken Word',
    'Contemporary Music',
    'Family',
    'Film',
    'Other',
    'Talks, Tours $ Learning',
    'Variety / Light Entertainment',
    'Video On Demand',
)

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
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def event_links(session):
    """Combine the site's complete, server-rendered category result feeds."""
    links = []
    seen = set()
    for category in EVENT_CATEGORIES:
        soup = get_soup(
            session,
            SEARCH_URL,
            params={
                'month': '',
                'cat': category,
                'ven': '',
                'daterangefrom': '',
                'daterangeto': '',
            },
        )
        for anchor in soup.select('a[href*="/whats-on/"]'):
            url = urljoin(SOURCE_URL, anchor.get('href', ''))
            path = urlparse(url).path.rstrip('/')
            if not re.search(r'/whats-on/[^/]+/[^/]+/\d+$', path) or url in seen:
                continue
            seen.add(url)
            links.append(url)
    return links


def parse_date(date_text, year):
    value = clean_text(date_text).rstrip(' .')
    if not value:
        return None
    if not re.search(r'\b\d{4}\b', value):
        value = f'{value} {year}'
    value = re.sub(r'^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?\s+', '', value, flags=re.I)
    for pattern in ('%d %b %Y', '%d %B %Y'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def parse_time(value):
    text = clean_text(value).lower().replace('.', ':').replace(' ', '')
    match = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?(am|pm)', text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3) == 'pm' and hour != 12:
        hour += 12
    if match.group(3) == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def fallback_occurrence(header_text):
    match = re.search(
        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+'
        r'(\d{1,2}\s+[A-Za-z]+\s+\d{4})\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm))',
        header_text,
        flags=re.I,
    )
    if not match:
        return []
    return [(parse_date(match.group(1), ''), parse_time(match.group(2)))]


def detail_records(session, url):
    soup = get_soup(session, url)
    banner = soup.select_one('.detailMain_left .listDetail_Banner')
    title = clean_text(banner.select_one('h2')) if banner else ''
    venue = clean_text(banner.select_one('.VenueText')) if banner else ''
    header_text = clean_text(banner.select_one('.DateText')) if banner else ''
    year_match = re.search(r'\b(20\d{2})\b', header_text)
    if not title or not venue or not year_match:
        return []

    occurrences = []
    seen = set()
    schedule = soup.select_one('#details .Scheduleticket')
    for item in schedule.select('li') if schedule else []:
        event_date = parse_date(item.select_one('.DateSpan'), year_match.group(1))
        times = [parse_time(node) for node in item.select('.TimeSpan')]
        time_from = next((value for value in times if value), None)
        occurrence = (event_date, time_from)
        if event_date and occurrence not in seen:
            seen.add(occurrence)
            occurrences.append(occurrence)
    if not occurrences:
        occurrences = fallback_occurrence(header_text)

    description_node = soup.select_one('#details .descriptionTabs')
    if description_node:
        for unwanted in description_node.select(
            '.Scheduleticket, script, style, iframe, .bookBtn_RT'
        ):
            unwanted.decompose()
    description = clean_text(description_node) or None

    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'GB',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from in occurrences
        if event_date
    ]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    links = event_links(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(detail_records, session, url): url for url in links}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class LiverpoolPhilComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='liverpoolphil_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
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
    LiverpoolPhilComCrawler().run()


if __name__ == '__main__':
    main()
