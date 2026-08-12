import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.armonico.org.uk/'
SOURCE = 'Armonico Consort'
SITEMAP_URL = f'{SOURCE_URL}performances-sitemap.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

PERFORMANCE_RE = re.compile(
    r'^\s*(\d{1,2})(?:st|nd|rd|th)\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'(20\d{2})\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*,\s*(.+?)\s*$',
    re.I,
)

# A few listings contain an extra comma inside the town or venue name. These
# exact corrections are backed by the site's own listing copy and venue names.
LOCATION_CORRECTIONS = {
    ("St Peter's Church , Budleigh", 'Salterton'):
        ("St Peter's Church", 'Budleigh Salterton'),
    ('Festival Theatre', 'Malvern Theatres'):
        ('Festival Theatre, Malvern Theatres', 'Malvern'),
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount(
        'https://',
        HTTPAdapter(max_retries=Retry(
            total=2,
            backoff_factor=0.75,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=('GET',),
        )),
    )
    return session


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def event_urls(session):
    soup = BeautifulSoup(get_response(session, SITEMAP_URL).content, 'xml')
    return list(dict.fromkeys(
        clean_text(node)
        for node in soup.select('url > loc')
        if re.fullmatch(r'https://www\.armonico\.org\.uk/whats-on/[^/]+/', clean_text(node))
    ))


def parse_performance(value):
    match = PERFORMANCE_RE.match(clean_text(value))
    if not match:
        return None
    try:
        event_date = datetime.strptime(
            f'{match.group(1)} {match.group(2)} {match.group(3)}',
            '%d %B %Y',
        ).date().isoformat()
    except ValueError:
        return None

    hour = int(match.group(4))
    minute = int(match.group(5) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(6).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(6).lower() == 'am' and hour == 12:
        hour = 0

    location = match.group(7).strip(' ,')
    if ',' not in location:
        return None
    venue, city = (part.strip(' ,') for part in location.rsplit(',', 1))
    venue, city = LOCATION_CORRECTIONS.get((venue, city), (venue, city))
    if not venue or not city:
        return None
    return event_date, f'{hour:02d}:{minute:02d}', venue, city


def event_description(soup):
    parts = []
    content = clean_text(soup.select_one('.entry-content'))
    if content:
        parts.append(content)

    summary = soup.select_one('.entry-summary-venues')
    if summary:
        performers = []
        for child in summary.children:
            if getattr(child, 'name', None) == 'h2':
                break
            text = clean_text(child)
            if text:
                performers.append(text)
        performer_text = '\n'.join(performers)
        if performer_text:
            parts.append(performer_text)
    return '\n\n'.join(parts) or None


def parse_event(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    title = clean_text(soup.select_one('main h1, article h1, h1.entry-title'))
    description = event_description(soup)
    records = []
    for item in soup.select('.entry-summary-venues li'):
        parsed = parse_performance(clean_text(item.select_one('strong')))
        if not title or not parsed:
            continue
        event_date, time_from, venue, city = parsed
        records.append({
            'title': title,
            'date': event_date,
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


class ArmonicoOrgUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='armonico_org_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = make_session()
        urls = event_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(get_response, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    parsed = parse_event(future.result().content, url)
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Armonico performance detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if not parsed:
                    log_message(
                        'Skipped Armonico page without complete performance occurrences',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='No occurrence with a valid date, time, venue, and city',
                    )
                records.extend(parsed)
        return sorted(records, key=lambda row: (
            row['date'], row['time_from'] or '', row['title'], row['venue'], row['city']
        ))


def main():
    ArmonicoOrgUkCrawler().run()


if __name__ == '__main__':
    main()
