import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://capeannsymphony.org/'
SOURCE = 'Cape Ann Symphony'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts')
ARCHIVE_URL = urljoin(SOURCE_URL, 'archive/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUE_CITIES = {
    'crowell chapel': 'Manchester-by-the-Sea',
    'dolan performing arts center': 'Ipswich',
    'ipswich high school': 'Ipswich',
    'merhs auditorium': 'Manchester-by-the-Sea',
    'manchester essex regional high school': 'Manchester-by-the-Sea',
    'st. paul lutheran church': 'Gloucester',
    'st. paul’s lutheran church': 'Gloucester',
    'gloucester unitarian universalist meetinghouse': 'Gloucester',
    'gloucester uu church': 'Gloucester',
    'annisquam village hall': 'Gloucester',
    'a private home in magnolia': 'Gloucester',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = str(value).replace('\xa0', ' ').replace('\u2009', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def fetch_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def city_for_venue(venue):
    normalized = clean_text(venue).casefold()
    return next((city for name, city in VENUE_CITIES.items() if name in normalized), None)


def detail_description(soup):
    parts = []
    for selector in ('.concert-lede', '.concert-program-wrap'):
        text = clean_text(soup.select_one(selector))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def detail_records(soup, url):
    description = detail_description(soup)
    records = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            item = json.loads(script.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        if item.get('@type') != 'Event':
            continue
        location = item.get('location') or {}
        address = location.get('address') or {}
        venue = clean_text(location.get('name'))
        city = clean_text(address.get('addressLocality')) or city_for_venue(venue)
        start = clean_text(item.get('startDate'))
        try:
            starts_at = datetime.fromisoformat(start)
        except ValueError:
            continue
        title = re.sub(r'^Cape Ann Symphony:\s*', '', clean_text(item.get('name')))
        if not all((title, venue, city)):
            continue
        records.append({
            'title': title,
            'date': starts_at.date().isoformat(),
            'url': url,
            'time_from': starts_at.strftime('%H:%M'),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description or clean_text(item.get('description')) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def archive_description(concert):
    lines = []
    for work in concert.get('programme') or []:
        composer = clean_text(work.get('composer'))
        piece = clean_text(work.get('piece'))
        line = ' — '.join(part for part in (composer, piece) if part)
        if line:
            lines.append(line)
    soloist = clean_text(concert.get('soloist'))
    if soloist:
        lines.append(soloist)
    return '\n'.join(lines) or None


def archive_record(concert):
    title = clean_text(concert.get('title'))
    venue = clean_text(concert.get('venue'))
    city = city_for_venue(venue)
    timestamp = concert.get('date_ts')
    if not all((title, venue, city, timestamp)):
        return None
    try:
        event_date = datetime.fromtimestamp(int(timestamp), timezone.utc).date().isoformat()
    except (ValueError, TypeError, OSError):
        return None
    slug = clean_text(concert.get('slug'))
    url = urljoin(SOURCE_URL, f'concerts/{slug}') if slug else ARCHIVE_URL
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': archive_description(concert),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def musicians_unleashed_records(soup):
    description = clean_text(soup.select_one('.mu-teaser__lede')) or None
    records = []
    for item in soup.select('.mu-teaser__dates .mu-date'):
        when = clean_text(item.select_one('.mu-date__when'))
        where = item.select_one('.mu-date__where')
        match = re.search(
            r'(January|February|March|April|May|June|July|August|September|October|November|December)'
            r'\s+(\d{1,2}),\s+(20\d{2}).*?(\d{1,2})(?::([0-5]\d))?\s*(am|pm)',
            when,
            re.IGNORECASE,
        )
        location = clean_text(where)
        if not match or not location:
            continue
        venue, separator, city = location.rpartition(',')
        if not separator:
            venue, city = location, city_for_venue(location)
        try:
            event_date = datetime.strptime(
                f'{match.group(1)} {match.group(2)}, {match.group(3)}', '%B %d, %Y'
            ).date().isoformat()
        except ValueError:
            continue
        hour = int(match.group(4)) % 12 + (12 if match.group(6).lower() == 'pm' else 0)
        if not city:
            continue
        records.append({
            'title': 'Musicians Unleashed',
            'date': event_date,
            'url': CONCERTS_URL,
            'time_from': f'{hour:02d}:{match.group(5) or "00"}',
            'venue': venue.strip(),
            'city': city.strip(),
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class CapeannsymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='capeannsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        concerts_soup = fetch_soup(session, CONCERTS_URL)
        archive_soup = fetch_soup(session, ARCHIVE_URL)

        data_node = archive_soup.select_one('#archive-data')
        if data_node is None or not data_node.string:
            raise ValueError('Cape Ann Symphony archive data was not found')
        archive = json.loads(data_node.string)
        concerts = [
            concert
            for season in archive.get('seasons') or []
            for concert in season.get('concerts') or []
        ]

        urls = {
            urljoin(SOURCE_URL, link['href'])
            for soup in (concerts_soup, archive_soup)
            for link in soup.select('a[href*="/concerts/"][href]')
        }
        detailed = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(fetch_soup, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    detailed.extend(detail_records(future.result(), url))
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Cape Ann Symphony concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = detailed + musicians_unleashed_records(concerts_soup)
        detailed_urls = {record['url'] for record in detailed}
        for concert in concerts:
            slug = clean_text(concert.get('slug'))
            url = urljoin(SOURCE_URL, f'concerts/{slug}') if slug else ARCHIVE_URL
            if url in detailed_urls:
                continue
            record = archive_record(concert)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    CapeannsymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
