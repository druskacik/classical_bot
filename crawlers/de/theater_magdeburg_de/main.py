import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.theater-magdeburg.de/'
SCHEDULE_URL = urljoin(SOURCE_URL, 'spielplan/spielplan/')
SOURCE = 'Theater Magdeburg'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

# These are the institution's Magdeburg venues. An unfamiliar location is not
# assigned to Magdeburg: it may be a touring performance and is skipped unless
# the displayed venue itself clearly names Magdeburg.
LOCAL_VENUE_PREFIXES = (
    'Opernhaus',
    'Schauspielhaus',
    'Kammer ',
    'Kasino',
    'Domplatz',
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def displayed_venue(link):
    location_column = link.find('div', recursive=False)
    if not location_column:
        return ''
    location_column = BeautifulSoup(str(location_column), 'html.parser')
    for node in location_column.select('.info'):
        node.decompose()
    text = clean_text(location_column.get_text(' ', strip=True))
    match = re.search(r'→\s*(.+)$', text)
    return match.group(1).strip() if match else ''


def resolve_location(venue):
    if not venue:
        return None
    if venue.startswith(LOCAL_VENUE_PREFIXES) or 'Magdeburg' in venue:
        return venue, 'Magdeburg'
    return None


def listing_description(link):
    content = link.select_one('.col12, .col-xxl-7')
    return clean_text(content.get_text('\n', strip=True)) if content else ''


def parse_line(line):
    script = line.select_one('script[type="application/ld+json"]')
    link = line.select_one('a.row[href]')
    if not script or not link:
        return None
    try:
        data = json.loads(script.string or script.get_text())
    except (json.JSONDecodeError, TypeError):
        return None

    title = clean_text(data.get('name'))
    url = urljoin(SOURCE_URL, data.get('url') or link.get('href', ''))
    location = resolve_location(displayed_venue(link))
    try:
        start = datetime.fromisoformat(data.get('startDate', ''))
    except (TypeError, ValueError):
        return None
    if not title or not url or not location:
        return None

    venue, city = location
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M') if 'T' in data.get('startDate', '') else None,
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': listing_description(link) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(url):
    soup = get_soup(url)
    section = soup.select_one('main section.ce-gridelements')
    return clean_text(section.get_text('\n', strip=True)) if section else None


def get_concerts():
    soup = get_soup(SCHEDULE_URL)
    records = []
    for line in soup.select('.day .line'):
        record = parse_line(line)
        if record:
            records.append(record)

    descriptions = {}
    urls = {record['url'] for record in records}
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(detail_description, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        detail = descriptions.get(record['url'])
        summary = record['description']
        if detail and summary and summary not in detail:
            record['description'] = f'{summary}\n\n{detail}'
        elif detail:
            record['description'] = detail

    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class TheaterMagdeburgDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theater_magdeburg_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    TheaterMagdeburgDeCrawler().run()


if __name__ == '__main__':
    main()
