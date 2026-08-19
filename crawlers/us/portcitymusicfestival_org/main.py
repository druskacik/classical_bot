import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.portcitymusicfestival.org/'
SOURCE = 'Port City Music Festival'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_HEADING_RE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Za-z]+\s+\d{1,2})\s+(\d{1,2}:\d{2}\s*[AP]M)(?:\s+ET)?$',
    re.IGNORECASE,
)
FESTIVAL_PATH_RE = re.compile(r'^/festival-(\d{4})(?:-|$)')
DETAIL_PATH_RE = re.compile(r'^/([a-z]+)-(\d{1,2})-(\d{4})/?$', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, parser='html.parser'):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, parser)


def discover_listing_url(session):
    sitemap = get_soup(session, SITEMAP_URL, parser='xml')
    candidates = []
    for location in sitemap.find_all('loc'):
        url = clean_text(location.get_text())
        match = FESTIVAL_PATH_RE.match(urlparse(url).path)
        if match:
            candidates.append((int(match.group(1)), urljoin(SOURCE_URL, urlparse(url).path)))

    if not candidates:
        raise ValueError('No annual festival listing found in sitemap')
    return max(candidates)[1]


def detail_urls_by_date(soup, listing_url):
    urls = {}
    for link in soup.find_all('a', href=True):
        url = urljoin(listing_url, link['href'])
        match = DETAIL_PATH_RE.match(urlparse(url).path)
        if not match:
            continue
        month, day, year = match.groups()
        try:
            date = datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
        except ValueError:
            continue
        urls[date] = url
    return urls


def event_row(heading):
    node = heading
    while node and node.name != 'body':
        if 'row' in (node.get('class') or []):
            return node
        node = node.parent
    return None


def parse_listing(soup, listing_url):
    year_match = FESTIVAL_PATH_RE.match(urlparse(listing_url).path)
    if not year_match:
        raise ValueError(f'Cannot determine festival year from {listing_url}')
    year = year_match.group(1)
    detail_urls = detail_urls_by_date(soup, listing_url)
    records = []

    for heading in soup.find_all('h1'):
        heading_text = clean_text(heading.get_text(' ', strip=True))
        match = DATE_HEADING_RE.match(heading_text)
        if not match:
            continue

        month_day, displayed_time = match.groups()
        try:
            event_date = datetime.strptime(f'{month_day} {year}', '%B %d %Y').date().isoformat()
            time_from = datetime.strptime(displayed_time.upper(), '%I:%M %p').strftime('%H:%M')
        except ValueError:
            continue

        row = event_row(heading)
        venue_heading = row.find('h2') if row else None
        venue = clean_text(venue_heading.get_text(' / ', strip=True)) if venue_heading else ''
        row_text = clean_text(row.get_text('\n', strip=True)) if row else ''
        city_match = re.search(r'^([^\n,]+),\s*NC\s+\d{5}(?:-\d{4})?$', row_text, re.MULTILINE)
        city = clean_text(city_match.group(1)) if city_match else ''
        if not venue or not city:
            log_message(
                'Skipping event without a usable venue or city',
                event='crawler_event_skipped',
                level='warning',
                url=listing_url,
                date=event_date,
            )
            continue

        is_masterclass = bool(re.search(r'\bCommunity Masterclass\b', row_text, re.IGNORECASE))
        title = (
            f'Community Masterclass at {venue}'
            if is_masterclass
            else f'Port City Music Festival at {venue}'
        )
        records.append({
            'title': title,
            'date': event_date,
            'url': detail_urls.get(event_date, listing_url),
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': row_text or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    listing_url = discover_listing_url(session)
    records = parse_listing(get_soup(session, listing_url), listing_url)
    if not records:
        log_message(
            'No festival performances found',
            event='crawler_empty_listing',
            level='warning',
            url=listing_url,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['venue']))


class PortCityMusicFestivalOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='portcitymusicfestival_org',
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
    PortCityMusicFestivalOrgCrawler().run()


if __name__ == '__main__':
    main()
