import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://eastneukfestival.com/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/events'
SOURCE = 'East Neuk Festival'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b([A-Z][a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,\s+(20\d{2})\b'
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', re.IGNORECASE)
CITY_NAMES = (
    'Anstruther',
    'Cellardyke',
    'Crail',
    'Kilrenny',
    'St Monans',
    'Pittenweem',
    'Elie',
    'Earlsferry',
    'Kingsbarns',
    'St Andrews',
    'Cupar',
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def event_urls(session):
    urls = []
    page = 1
    while True:
        response = get_response(
            session,
            API_URL,
            params={'per_page': 100, 'page': page, '_fields': 'link'},
        )
        urls.extend(item['link'] for item in response.json() if item.get('link'))
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return list(dict.fromkeys(urls))


def parse_date(text):
    match = DATE_RE.search(text)
    if not match:
        return None
    month, day, year = match.groups()
    try:
        return datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def extract_city(venue, address):
    evidence = f'{venue}\n{address}'
    for city in CITY_NAMES:
        if re.search(rf'\b{re.escape(city)}\b', evidence, re.IGNORECASE):
            return city
    return None


def parse_event(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    title = clean_text(soup.select_one('.artist-title h1'))
    event_line = clean_text(soup.select_one('.artist-title p'))
    venue = clean_text(soup.select_one('.artist-venue-data h3'))
    address = clean_text(soup.select_one('.artist-venue-address'))
    event_date = parse_date(event_line)
    city = extract_city(venue, address)
    if not title or not event_date or not venue or not city:
        return None

    description = clean_text(soup.select_one('.content')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(event_line),
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(get_response, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_event(future.result().content, url)
                if record:
                    records.append(record)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape East Neuk Festival event detail',
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


class EastNeukFestivalComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='eastneukfestival_com',
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
    EastNeukFestivalComCrawler().run()


if __name__ == '__main__':
    main()
