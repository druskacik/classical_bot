import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

import requests
import urllib3
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://moabmusicfest.org/'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/wp/v2/mec-events'
SOURCE = 'Moab Music Festival'
DEFAULT_CITY = 'Moab'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# The source currently serves an expired certificate.  Disabling verification is
# necessary until the publisher renews it; requests are still made only to the
# fixed source host returned by its own REST API.
VERIFY_TLS = False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(html.unescape(str(value)), 'html.parser')
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(value):
    parts = urlsplit(value or '')
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip('/') + '/', '', ''))


def fetch_event_posts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1
    while True:
        response = session.get(
            EVENTS_API_URL,
            params={'per_page': 100, 'page': page, '_fields': 'id,link,title'},
            timeout=45,
            verify=VERIFY_TLS,
        )
        response.raise_for_status()
        batch = response.json()
        records.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            return records
        page += 1


def event_schema(soup, event_url):
    wanted = canonical_url(event_url)
    matches = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        values = data if isinstance(data, list) else [data]
        for value in values:
            if not isinstance(value, dict) or value.get('@type') != 'Event':
                continue
            if canonical_url(value.get('url')) == wanted:
                return value
            matches.append(value)
    return matches[-1] if matches else None


def parse_time(soup):
    node = soup.select_one('.mec-single-event-time .mec-events-abbr')
    value = clean_text(node) if node else ''
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value.upper(), pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_description(soup):
    parts = []
    for selector in ('#event-description-tab', '#artists-program-tab'):
        node = soup.select_one(selector)
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def infer_city(title, venue, description):
    evidence = ' '.join(filter(None, (title, venue, description)))
    if re.search(r'Grand America(?: Hotel)?|Salt Lake City', evidence, re.I):
        return 'Salt Lake City'
    return DEFAULT_CITY


def parse_event_page(post, session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    url = post.get('link', '')
    if urlsplit(url).netloc.lower() not in {'moabmusicfest.org', 'www.moabmusicfest.org'}:
        return None
    response = session.get(url, timeout=45, verify=VERIFY_TLS)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    schema = event_schema(soup, url)
    if not schema:
        return None

    title = clean_text(schema.get('name')) or clean_text(post.get('title', {}).get('rendered'))
    title = re.sub(r'\s+', ' ', title).strip()
    date_value = str(schema.get('startDate', ''))[:10]
    try:
        event_date = datetime.strptime(date_value, '%Y-%m-%d').date().isoformat()
    except ValueError:
        return None

    location = schema.get('location') if isinstance(schema.get('location'), dict) else {}
    venue = clean_text(location.get('name'))
    description = parse_description(soup)
    city = infer_city(title, venue, description)
    if not all((title, event_date, url, venue, city)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(soup),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts():
    posts = fetch_event_posts()
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(parse_event_page, post): post for post in posts}
        for future in as_completed(futures):
            post = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Event page request failed',
                    event='crawler_event_request_failed',
                    level='warning',
                    url=post.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    if not records:
        log_message(
            'No parseable events found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_API_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class MoabMusicFestOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='moabmusicfest_org',
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
    MoabMusicFestOrgCrawler().run()


if __name__ == '__main__':
    main()
