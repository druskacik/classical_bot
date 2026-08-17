import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://pittsburghopera.org/'
SOURCE = 'Pittsburgh Opera'
ARCHIVE_START = (2015, 7)
FUTURE_MONTHS = 18
MAX_WORKERS = 6

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (compatible; ClassicalConcertCrawler/1.0; '
        '+https://github.com/openai/)'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

CITY_LOCATIONS = {
    'pittsburgh': 'Pittsburgh',
    'sewickley': 'Sewickley',
    'wexford': 'Wexford',
    'greensburg': 'Greensburg',
    'washington, pa': 'Washington',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def month_sequence(start, end):
    year, month = start
    while (year, month) <= end:
        yield year, month
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1


def add_months(year, month, amount):
    index = year * 12 + month - 1 + amount
    return index // 12, index % 12 + 1


def get_response(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response


def parse_date(text):
    match = re.search(
        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
        r'([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})',
        text,
    )
    if not match:
        return None
    try:
        return datetime.strptime(
            f'{match.group(1)} {match.group(2)} {match.group(3)}', '%B %d %Y'
        ).date().isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = re.search(r'\b(\d{1,2}):(\d{2})\s*([AP]M)\b', text, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).upper() == 'PM':
        hour += 12
    return f'{hour:02d}:{match.group(2)}'


def listing_items(year, month):
    url = f'{SOURCE_URL}calendar/month/{year:04d}-{month:02d}-01/'
    soup = BeautifulSoup(get_response(url).text, 'html.parser')
    items = []
    for element in soup.select('.event-item'):
        link = element.select_one('a[href]')
        title_node = element.select_one('h3')
        strong = element.select_one('strong')
        if not link or not title_node or not strong:
            continue
        detail_url = urljoin(SOURCE_URL, link.get('href', '').strip())
        title = clean_text(title_node)
        event_date = parse_date(clean_text(strong))
        if not title or not detail_url or not event_date:
            continue
        location_parts = []
        for sibling in strong.parent.next_siblings:
            if getattr(sibling, 'name', None) in ('br', 'a'):
                if getattr(sibling, 'name', None) == 'a':
                    break
                continue
            value = clean_text(sibling)
            if value:
                location_parts.append(value)
        items.append({
            'title': title,
            'date': event_date,
            'time_from': parse_time(clean_text(strong)),
            'location': ' '.join(location_parts).strip(),
            'url': detail_url,
        })
    return items


def detail_description(url):
    soup = BeautifulSoup(get_response(url).text, 'html.parser')
    container = soup.select_one('.main-content .event-items') or soup.select_one('.main-content')
    return clean_text(container) or None


def location_fields(location, description):
    location = clean_text(location)
    lowered = location.lower().strip(' .')
    city = 'Pittsburgh'
    for marker, value in CITY_LOCATIONS.items():
        if marker in lowered:
            city = value
            break

    # Parenthetical street addresses are useful evidence but are not venue names.
    venue = re.sub(r'\s*\([^)]*\d{3,}[^)]*\)\s*$', '', location).strip(' ,-')
    if lowered in CITY_LOCATIONS:
        if description and re.search(r'\b(private )?(home|residence)\b', description, re.I):
            venue = 'Private residence'
        else:
            return None, city
    if not venue or venue.upper() == 'TBD':
        return None, city
    return venue, city


def get_concerts():
    today = date.today()
    end = add_months(today.year, today.month, FUTURE_MONTHS)
    months = list(month_sequence(ARCHIVE_START, end))
    listings = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(listing_items, year, month): (year, month)
            for year, month in months
        }
        for future in as_completed(futures):
            year, month = futures[future]
            try:
                listings.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Pittsburgh Opera calendar month',
                    event='crawler_page_failed', level='warning',
                    url=f'{SOURCE_URL}calendar/month/{year:04d}-{month:02d}-01/',
                    error_type=type(error).__name__, error_message=str(error),
                )

    # The same occurrence can appear on two adjacent calendar range pages.
    listings = list({
        (item['url'], item['date'], item['time_from']): item for item in listings
    }.values())
    descriptions = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(detail_description, url): url for url in {x['url'] for x in listings}}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                descriptions[url] = None
                log_message(
                    'Failed to scrape Pittsburgh Opera event detail',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )

    records = []
    for item in listings:
        description = descriptions.get(item['url'])
        venue, city = location_fields(item['location'], description)
        if not venue:
            continue
        records.append({
            'title': item['title'], 'date': item['date'], 'url': item['url'],
            'time_from': item['time_from'], 'venue': venue, 'city': city,
            'country_code': 'US', 'description': description,
            'source_url': SOURCE_URL, 'source': SOURCE,
        })
    return sorted(records, key=lambda x: (x['date'], x['time_from'] or '', x['title']))


class PittsburghOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='pittsburghopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    PittsburghOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
