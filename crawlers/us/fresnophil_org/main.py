import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://fresnophil.org/'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/wp/v2/events'
SOURCE = 'Fresno Philharmonic'
CITY = 'Fresno'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/html;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Za-z]+\s+\d{1,2},\s+\d{4})'
    r'(?:\s*\|\s*(\d{1,2}(?::\d{2})?\s*[ap]m))?$',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_date_time(value):
    match = DATE_TIME_RE.match(clean_text(value))
    if not match:
        return None

    date_text, time_text = match.groups()
    try:
        event_date = datetime.strptime(date_text, '%B %d, %Y').date().isoformat()
    except ValueError:
        return None

    time_from = None
    if time_text:
        for pattern in ('%I:%M %p', '%I %p'):
            try:
                time_from = datetime.strptime(time_text.upper(), pattern).strftime('%H:%M')
                break
            except ValueError:
                continue
    return event_date, time_from


def artistic_description(soup):
    parts = []

    artists = []
    for node in soup.select('.event-artist-inner-wrapper'):
        value = clean_text(node.get_text(' ', strip=True))
        if value and value not in artists:
            artists.append(value)
    if artists:
        parts.append('Artists: ' + '; '.join(artists))

    programme = []
    wrapper = soup.select_one('.event-composer-wrapper')
    if wrapper:
        composer = None
        for node in wrapper.select('.event-composer, .event-composition'):
            value = clean_text(node.get_text(' ', strip=True))
            if not value:
                continue
            classes = node.get('class', [])
            if 'event-composer' in classes:
                composer = value
            elif composer:
                programme.append(f'{composer}: {value}')
            else:
                programme.append(value)
    if programme:
        parts.append('Programme: ' + '; '.join(programme))

    description = clean_text(
        ' '.join(node.get_text(' ', strip=True) for node in soup.select('.event-description'))
    )
    if description:
        parts.append(description)

    return '\n\n'.join(parts) or None


def parse_event(item, session=None):
    session = session or requests
    url = item.get('link', '')
    try:
        response = session.get(url, headers=HEADERS, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Event detail request failed',
            event='crawler_detail_request_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []

    soup = BeautifulSoup(response.text, 'html.parser')
    title_node = soup.select_one('.event-single-name')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    if not title:
        title = clean_text(item.get('title', {}).get('rendered', ''))

    venue_node = soup.select_one('.event-single-location')
    venue = clean_text(venue_node.get_text(' ', strip=True) if venue_node else '')
    if not title or not venue:
        soup.decompose()
        return []

    description = artistic_description(soup)
    records = []
    for date_node in soup.select('.event-single-date'):
        parsed = parse_date_time(date_node.get_text(' ', strip=True))
        if not parsed:
            continue
        event_date, time_from = parsed
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    soup.decompose()
    return records


def fetch_event_posts(session=None):
    session = session or requests.Session()
    posts = []
    page = 1
    while True:
        response = session.get(
            EVENTS_API_URL,
            headers=HEADERS,
            params={
                'per_page': 100,
                'page': page,
                '_fields': 'id,link,title',
            },
            timeout=45,
        )
        response.raise_for_status()
        posts.extend(response.json())
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return posts
        page += 1


def scrape_concerts(session=None):
    session = session or requests.Session()
    try:
        posts = fetch_event_posts(session)
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Events API request failed',
            event='crawler_listing_request_failed',
            level='error',
            url=EVENTS_API_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    records = []
    # Pages contain a sizeable shared WordPress layout, so keep concurrency
    # modest to bound memory while still avoiding a slow serial catalogue run.
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(parse_event, item) for item in posts]
        for future in as_completed(futures):
            records.extend(future.result())

    if not records:
        log_message(
            'No concrete event occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_API_URL,
            record_count=0,
        )

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class FresnoPhilOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fresnophil_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        return scrape_concerts()


def main():
    FresnoPhilOrgCrawler().run()


if __name__ == '__main__':
    main()
