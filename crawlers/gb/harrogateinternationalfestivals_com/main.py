import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://harrogateinternationalfestivals.com/'
SOURCE = 'Harrogate International Festivals'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'
POST_TYPES = ('events', 'past-events')
CITY = 'Harrogate'
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
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def api_urls(session, post_type):
    urls = set()
    page = 1
    while True:
        response = session.get(
            f'{API_URL}/{post_type}',
            params={'per_page': 100, 'page': page, '_fields': 'link'},
            timeout=45,
        )
        response.raise_for_status()
        urls.update(item['link'] for item in response.json() if item.get('link'))
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return urls


def parse_date(value):
    value = clean_text(value)
    value = re.sub(r'(?<=\d)(st|nd|rd|th)\b', '', value, flags=re.I)
    match = re.search(
        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*'
        r'(\d{1,2}\s+[A-Za-z]+\s+\d{4})',
        value,
        re.I,
    )
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%d %B %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', value, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def parse_location(soup):
    element = soup.select_one('.when_where .location')
    location = clean_text(element)
    if not location:
        return None
    # Some pages add a second promotional .location; the first is the venue.
    location = location.split('\n', 1)[0].strip(' ,')
    parts = [part.strip() for part in location.rsplit(',', 1)]
    if len(parts) == 2 and parts[1].casefold() == CITY.casefold():
        return parts[0], CITY
    # This is a Harrogate-based venue calendar. Comma suffixes such as
    # "Crescent Gardens" are venue sub-locations, not cities.
    return location, CITY


def detail_records(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    title = clean_text(soup.select_one('.show_info .title'))
    location = parse_location(soup)
    description = clean_text(soup.select_one('.show_details')) or None
    if not title or not location:
        return []

    venue, city = location
    records = []
    seen = set()
    for date_element in soup.select('.show_info .date'):
        date_text = clean_text(date_element)
        date = parse_date(date_text)
        if not date or date in seen:
            continue
        seen.add(date)
        time_from = parse_time(clean_text(date_element.parent))
        records.append({
            'title': title,
            'date': date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'GB',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = set()
    for post_type in POST_TYPES:
        try:
            urls.update(api_urls(session, post_type))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Harrogate event index',
                event='crawler_index_failed',
                level='warning',
                url=f'{API_URL}/{post_type}',
                error_type=type(error).__name__,
                error_message=str(error),
            )

    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(detail_records, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Harrogate event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class HarrogateInternationalFestivalsComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='harrogateinternationalfestivals_com',
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
        return get_concerts()


def main():
    HarrogateInternationalFestivalsComCrawler().run()


if __name__ == '__main__':
    main()
