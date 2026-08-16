import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://chelseasymphony.org/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts/')
SOURCE = 'The Chelsea Symphony'
CITY = 'New York'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def is_concert_detail_url(url):
    path = urlparse(url).path.rstrip('/')
    return bool(re.fullmatch(r'/concerts/\d{4}-\d{4}/[^/]+', path))


def discover_detail_urls(session):
    listing = get_soup(session, CONCERTS_URL)
    season_urls = {
        urljoin(CONCERTS_URL, link['href'])
        for link in listing.select('.concert-season-list-container a[href]')
    }

    detail_urls = {
        urljoin(CONCERTS_URL, link['href'])
        for link in listing.select('article h2 a[href]')
        if is_concert_detail_url(urljoin(CONCERTS_URL, link['href']))
    }
    for season_url in sorted(season_urls):
        try:
            season = get_soup(session, season_url)
        except requests.RequestException as error:
            log_message(
                'Could not load concert season',
                event='crawler_season_error',
                level='warning',
                url=season_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        detail_urls.update(
            urljoin(season_url, link['href'])
            for link in season.select('article h2 a[href]')
            if is_concert_detail_url(urljoin(season_url, link['href']))
        )
    return sorted(detail_urls)


def venue_for_occurrence(location, event_date):
    location = clean_text(location)
    if not location or re.search(r'\bonline\b|https?://', location, re.I):
        return ''

    # One archived concert explicitly assigns a different venue to each date.
    dated_venues = re.findall(r'([^,]+?)\s*\((\d{1,2})/(\d{1,2})\)', location)
    if dated_venues:
        month = str(event_date.month)
        day = str(event_date.day)
        for venue, venue_month, venue_day in dated_venues:
            if (venue_month, venue_day) == (month, day):
                return clean_text(venue)
        return ''

    venue = location.split(',', 1)[0]
    venue = re.sub(r'\s+\d{1,5}\s+(?:W\.?|West)\s+\d+.*$', '', venue, flags=re.I)
    venue = re.sub(r'\s+\d{1,5}\s+West\s+\d+.*$', '', venue, flags=re.I)
    venue = re.sub(r'\s+9th Street\s*&\s*Prospect Park West$', '', venue, flags=re.I)
    canonical_venues = {
        "st. paul's church": "St. Paul's Church",
        'the dimenna center': 'The DiMenna Center',
        'the dimenna center for classical music': 'The DiMenna Center for Classical Music',
    }
    venue = clean_text(venue)
    return canonical_venues.get(venue.lower(), venue)


def description_from_soup(soup):
    parts = []
    copy = soup.select_one('.concert-copy .block-paragraph')
    if copy:
        text = clean_text(copy)
        if text:
            parts.append(text)

    program_items = []
    for item in soup.select('.concert-program-listing'):
        text = clean_text(item.select_one('.concert-program-piece'))
        if text and text not in program_items:
            program_items.append(text)
    if program_items:
        parts.append('Program:\n' + '\n'.join(program_items))
    return '\n\n'.join(parts) or None


def parse_detail(soup, url):
    title = clean_text(soup.select_one('h1.concert-name'))
    location = clean_text(soup.select_one('.concert-location'))
    if not title or not location:
        return []

    description = description_from_soup(soup)
    records = []
    for node in soup.select('.concert-date .date-display-single[content]'):
        raw_datetime = node.get('content', '')
        try:
            occurrence = datetime.fromisoformat(raw_datetime)
        except ValueError:
            continue
        venue = venue_for_occurrence(location, occurrence)
        if not venue:
            continue
        records.append({
            'title': title,
            'date': occurrence.date().isoformat(),
            'url': url,
            'time_from': occurrence.strftime('%H:%M'),
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    detail_urls = discover_detail_urls(session)
    records = []

    def fetch(url):
        return url, parse_detail(get_soup(session, url), url)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch, url): url for url in detail_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                _, detail_records = future.result()
                records.extend(detail_records)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Could not parse concert detail',
                    event='crawler_detail_error',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=CONCERTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class ChelseaSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chelseasymphony_org',
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
        return scrape_concerts()


def main():
    ChelseaSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
