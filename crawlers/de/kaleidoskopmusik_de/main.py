import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://kaleidoskopmusik.de/'
DATES_URL = urljoin(SOURCE_URL, 'en/dates/')
SOURCE = 'Solistenensemble Kaleidoskop'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9,de;q=0.7',
}

# The dates page omits the city for a number of well-known venues. These
# defaults are limited to venues whose location is unambiguous.
LOCATIONS = {
    'halle am berghain': ('Berlin', 'DE'),
    'radialsystem': ('Berlin', 'DE'),
    'fft düsseldorf': ('Düsseldorf', 'DE'),
    'silent green': ('Berlin', 'DE'),
    'hessisches staat': ('Wiesbaden', 'DE'),
    'st. matthäus-kirche': ('Berlin', 'DE'),
    'heilige-geist-kirche moabit': ('Berlin', 'DE'),
    'haus der berliner festspiele': ('Berlin', 'DE'),
    'volksbühne am rosa-luxemburg-platz': ('Berlin', 'DE'),
    'berliner medizinhistorisches museum': ('Berlin', 'DE'),
    'staatsschauspiel dresden': ('Dresden', 'DE'),
    'musikinstrumenten-museum': ('Berlin', 'DE'),
    'von krahl theatre': ('Tallinn', 'EE'),
    'amare': ('The Hague', 'NL'),
    'hau 2': ('Berlin', 'DE'),
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(r'(\d{2}/\d{2}/\d{4})', value)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%d/%m/%Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2}:\d{2})\s*([ap]m)\b', value, re.I)
    if not match:
        return None
    try:
        return datetime.strptime(' '.join(match.groups()), '%I:%M %p').strftime('%H:%M')
    except ValueError:
        return None


def resolve_location(venue):
    normalized = venue.casefold()
    for marker, location in LOCATIONS.items():
        if marker in normalized:
            return location
    return None


def event_url(block):
    project_link = block.select_one(
        'a[href*="/projects/"], a.round-button[href*="/projects/"]'
    )
    return urljoin(DATES_URL, project_link['href']) if project_link else DATES_URL


def parse_current(block):
    title = clean_text(block.select_one('.title'))
    date_nodes = block.select('.date')
    event_date = parse_date(clean_text(date_nodes[0])) if date_nodes else None
    time_from = parse_time(clean_text(date_nodes[1])) if len(date_nodes) > 1 else None
    venue = clean_text(block.select_one('.location'))
    location = resolve_location(venue)
    url = event_url(block)
    if not title or not event_date or not venue or not location or not url:
        return None
    description = clean_text(block.select_one('.text')) or None
    city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_past(block):
    title = clean_text(block.select_one('.past-termine-block-title'))
    event_date = parse_date(clean_text(block.select_one('.past-termine-block-date')))
    time_from = parse_time(clean_text(block.select_one('.past-termine-block-time')))
    venue = clean_text(block.select_one('.past-termine-block-location'))
    location = resolve_location(venue)
    url = urljoin(DATES_URL, block.get('href', ''))
    if not title or not event_date or not venue or not location or not url:
        return None
    city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_description(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    return clean_text(soup.select_one('.main.project .content')) or None


class KaleidoskopmusikDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kaleidoskopmusik_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        try:
            response = session.get(DATES_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Kaleidoskop events',
                event='crawler_fetch_failed',
                level='error',
                url=DATES_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = [parse_current(block) for block in soup.select('.termin-block')]
        records.extend(parse_past(block) for block in soup.select('a.past-termine-block'))
        records = [record for record in records if record]
        records = list({
            (record['title'], record['date'], record['time_from'], record['venue']): record
            for record in records
        }.values())

        descriptions = {}
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(fetch_description, session, url): url
                for url in {record['url'] for record in records if '/projects/' in record['url']}
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    descriptions[url] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Kaleidoskop project description',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        for record in records:
            record['description'] = descriptions.get(record['url']) or record['description']

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    KaleidoskopmusikDeCrawler().run()


if __name__ == '__main__':
    main()
