import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cnz.ch/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts')
SOURCE = 'Collegium Novum Zürich'

HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'de-CH,de;q=0.9,en;q=0.7',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

SPECIAL_LOCATIONS = {
    'Huddersfield, Town Hall': ('Town Hall', 'Huddersfield', 'GB'),
    'Lausanne, Utopia I': ('Utopia I', 'Lausanne', 'CH'),
    'Koninklijk Theater Carré, Amsterdam': (
        'Koninklijk Theater Carré', 'Amsterdam', 'NL'
    ),
}

CITY_ALIASES = {
    'Genf': 'Genève',
    'Geneva': 'Genève',
    'Zurich': 'Zürich',
}

GERMAN_MONTHS = {
    'Januar': 1, 'Februar': 2, 'März': 3, 'April': 4, 'Mai': 5, 'Juni': 6,
    'Juli': 7, 'August': 8, 'September': 9, 'Oktober': 10,
    'November': 11, 'Dezember': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response


def parse_location(node):
    if not node:
        return None

    location = node.select_one('.location') or node
    parts = [
        clean_text(child)
        for child in location.find_all('span', recursive=False)
        if 'at' not in (child.get('class') or [])
        and 'city' not in (child.get('class') or [])
    ]
    location_name = ' '.join(part for part in parts if part).strip()
    if not location_name:
        return None

    if location_name in SPECIAL_LOCATIONS:
        return SPECIAL_LOCATIONS[location_name]

    city_node = location.select_one('.city')
    address = clean_text(city_node).strip('() ') if city_node else ''

    if ':' in location_name:
        city, venue = (part.strip() for part in location_name.split(':', 1))
    else:
        venue = location_name
        postal_city = re.search(r'\b\d{4}\s+([^,)]+)', address)
        city = postal_city.group(1).strip() if postal_city else ''

    city = CITY_ALIASES.get(city, city)
    if not venue or not city:
        return None
    return venue, city, 'CH'


def event_dates(date_node):
    time_node = date_node.select_one('time[datetime]') if date_node else None
    if time_node:
        match = re.match(r'(\d{4}-\d{2}-\d{2})', time_node.get('datetime', ''))
        return [match.group(1)] if match else []

    text = clean_text(date_node)
    match = re.search(
        r'((?:\d{1,2}\./)*\d{1,2})\.\s+'
        r'(Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)'
        r'\s+(\d{4})',
        text,
    )
    if not match:
        return []
    days = [int(value) for value in re.findall(r'\d{1,2}', match.group(1))]
    month = GERMAN_MONTHS[match.group(2)]
    year = int(match.group(3))
    try:
        return [date(year, month, day).isoformat() for day in days]
    except ValueError:
        return []


def event_times(date_node):
    text = clean_text(date_node)
    suffix = text.rsplit('-', 1)[-1] if '-' in text else text
    return list(dict.fromkeys(
        f'{int(hour):02d}:{minute}'
        for hour, minute in re.findall(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', suffix)
    )) or [None]


def listing_records(article):
    link = article.select_one('.title a[href]')
    title = clean_text(link)
    date_node = article.select_one('.field-date')
    if not link or not title or not date_node:
        return []

    dates = event_dates(date_node)
    times = event_times(date_node)
    location = parse_location(article.select_one('.field-location'))
    if not dates or not location:
        return []

    venue, city, country_code = location
    program = clean_text(article.select_one('.field-concert-credits')) or None
    common = {
        'title': title,
        'url': urljoin(SOURCE_URL, link.get('href')),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': program,
    }
    return [
        {**common, 'date': event_date, 'time_from': time_from}
        for event_date in dates
        for time_from in times
    ]


def detail_description(html, fallback):
    soup = BeautifulSoup(html, 'html.parser')
    parts = []
    body = clean_text(soup.select_one('main .field-text'))
    program = clean_text(soup.select_one('main .field-concert-credits'))
    for part in (body, program, fallback or ''):
        if part and part not in parts:
            parts.append(part)
    return '\n\n'.join(parts) or None


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    listing = BeautifulSoup(get_response(session, CONCERTS_URL).text, 'html.parser')
    records = [
        record
        for article in listing.select('main article.concert.teaser')
        for record in listing_records(article)
    ]

    records_by_url = {}
    for record in records:
        records_by_url.setdefault(record['url'], []).append(record)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(get_response, session, url): (url, url_records)
            for url, url_records in records_by_url.items()
        }
        for future in as_completed(futures):
            url, url_records = futures[future]
            try:
                html = future.result().text
                for record in url_records:
                    record['description'] = detail_description(
                        html, record['description']
                    )
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape CNZ concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    log_message(
        'CNZ concert archive parsed',
        event='crawler_scrape_completed',
        url=CONCERTS_URL,
        record_count=len(records),
    )
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class CnzChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cnz_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    CnzChCrawler().run()


if __name__ == '__main__':
    main()
