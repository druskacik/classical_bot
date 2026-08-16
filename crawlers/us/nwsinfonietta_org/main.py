import re
from datetime import datetime
from html import unescape
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://nwsinfonietta.org/'
SOURCE = 'Northwest Sinfonietta'
SEASON_URL = urljoin(SOURCE_URL, 'concert')
COMMUNITY_URL = urljoin(SOURCE_URL, 'community-and-youth-events')
LOCAL_TIMEZONE = ZoneInfo('America/Los_Angeles')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_LINE_RE = re.compile(
    r'\b(?P<month>JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUN(?:E)?|'
    r'JUL(?:Y)?|AUG(?:UST)?|SEP(?:T(?:EMBER)?)?|OCT(?:OBER)?|NOV(?:EMBER)?|'
    r'DEC(?:EMBER)?)\s+(?P<day>\d{1,2})\s*\|\s*'
    r'(?P<time>\d{1,2}(?::\d{2})?\s*[AP]M)\s+'
    r'(?P<venue>[^,\n]+),\s*(?P<city>[^,\n]+),\s*WA\b',
    re.IGNORECASE,
)
SEASON_RE = re.compile(r'\b(20\d{2})\s*[-–]\s*(?:20)?(\d{2})\s+SEASON\b', re.I)


def clean_text(value):
    if not value:
        return ''
    text = unescape(str(value)).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    match = re.search(r'(\d{1,2})(?::(\d{2}))?\s*([AP])M', value, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).upper() == 'P' else 0)
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def season_years(soup):
    match = SEASON_RE.search(clean_text(soup.get_text(' ', strip=True)))
    if not match:
        raise ValueError('Could not determine the season years')
    return int(match.group(1)), 2000 + int(match.group(2))


def parse_season_page(html):
    soup = BeautifulSoup(html, 'html.parser')
    start_year, end_year = season_years(soup)
    records = []

    for block in soup.select('.sqs-block-html'):
        title_node = block.find('h1')
        details_node = block.find('h3')
        if not title_node or not details_node:
            continue
        details = clean_text(details_node.get_text('\n', strip=True))
        occurrences = list(DATE_LINE_RE.finditer(details))
        if not occurrences:
            continue

        title = clean_text(title_node.get_text(' ', strip=True))
        description = clean_text(block.get_text('\n', strip=True)) or None
        for occurrence in occurrences:
            month = datetime.strptime(occurrence.group('month')[:3], '%b').month
            year = start_year if month >= 7 else end_year
            try:
                event_date = datetime(year, month, int(occurrence.group('day'))).date().isoformat()
            except ValueError:
                continue
            venue = re.sub(r'\s*\(TBC\)\s*$', '', clean_text(occurrence.group('venue')), flags=re.I)
            city = clean_text(occurrence.group('city'))
            if not title or not venue or not city:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': SEASON_URL,
                'time_from': parse_time(occurrence.group('time')),
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def city_from_location(location):
    line = clean_text(location.get('addressLine2'))
    if not line:
        return None
    city = line.split(',', 1)[0].strip()
    return city or None


def parse_community_item(item):
    title = clean_text(item.get('title'))
    path = item.get('fullUrl')
    start = item.get('startDate')
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)
    if not title or not path or not start or not venue or not city:
        return None

    try:
        occurrence = datetime.fromtimestamp(int(start) / 1000, tz=LOCAL_TIMEZONE)
    except (TypeError, ValueError, OSError):
        return None
    description = clean_text(
        BeautifulSoup(item.get('body') or item.get('excerpt') or '', 'html.parser').get_text('\n')
    ) or None
    return {
        'title': title,
        'date': occurrence.date().isoformat(),
        'url': urljoin(SOURCE_URL, path),
        'time_from': occurrence.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_json(session, url):
    response = session.get(url, params={'format': 'json'}, timeout=45)
    response.raise_for_status()
    return response.json()


def scrape_community(session):
    records = []
    url = COMMUNITY_URL
    seen_urls = set()
    while url and url not in seen_urls:
        seen_urls.add(url)
        payload = fetch_json(session, url)
        for item in payload.get('upcoming', []) + payload.get('past', []):
            record = parse_community_item(item)
            if record:
                records.append(record)
        next_path = (payload.get('pagination') or {}).get('nextPageUrl')
        url = urljoin(SOURCE_URL, next_path) if next_path else None
    return records


class NwsinfoniettaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nwsinfonietta_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(SEASON_URL, timeout=45)
            response.raise_for_status()
            records = parse_season_page(response.text)
            records.extend(scrape_community(session))
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Northwest Sinfonietta scrape failed',
                event='crawler_request_failed',
                level='error',
                url=SEASON_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        finally:
            session.close()

        if not records:
            log_message(
                'No Northwest Sinfonietta events found',
                event='crawler_empty_listing',
                level='warning',
                url=SOURCE_URL,
                record_count=0,
            )
        unique = {
            (item['title'], item['date'], item['time_from'], item['venue']): item
            for item in records
        }
        return sorted(
            unique.values(),
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
        )


def main():
    NwsinfoniettaOrgCrawler().run()


if __name__ == '__main__':
    main()
