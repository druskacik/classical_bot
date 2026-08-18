import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.chambermusicabq.com/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts')
SITEMAP_URL = urljoin(SOURCE_URL, 'pages-sitemap.xml')
SOURCE = 'Chamber Music Albuquerque'
CITY = 'Albuquerque'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY)\s*-\s*'
    r'(?P<month>January|February|March|April|May|June|July|August|September|October|'
    r'November|December)\s+(?P<day>\d{1,2}),\s+(?P<year>20\d{2})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\bAT\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[AP]M)\b', re.I)

VENUES = {
    'simms center for the performing arts': 'Simms Center for the Performing Arts',
    'congregation albert': 'Congregation Albert',
    "st. john's united methodist church": "St. John's United Methodist Church",
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u200b', '').replace('\ufeff', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(text):
    match = DATE_RE.search(text)
    if not match:
        return None
    try:
        return datetime.strptime(
            f"{match.group('month')} {match.group('day')} {match.group('year')}",
            '%B %d %Y',
        ).date().isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    try:
        return datetime.strptime(
            f"{match.group('hour')}:{match.group('minute') or '00'} {match.group('ampm')}",
            '%I:%M %p',
        ).strftime('%H:%M')
    except ValueError:
        return None


def select_venue(text):
    lowered = text.lower()
    for marker, venue in VENUES.items():
        if marker in lowered:
            return venue
    return None


def make_record(title, date_text, venue_text, url, description):
    title = re.sub(r'\s+', ' ', clean_text(title)).strip()
    date_text = clean_text(date_text)
    venue = select_venue(clean_text(venue_text))
    event_date = parse_date(date_text)
    if not title or not event_date or not venue or not url:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(date_text),
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': clean_text(description) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_concerts_page(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for section in soup.select('main section[id]'):
        headings = section.find_all('h2')
        date_heading = next((h for h in headings if DATE_RE.search(clean_text(h))), None)
        if date_heading is None:
            continue
        title_heading = next(
            (
                h for h in headings
                if h is not date_heading and clean_text(h).lower() != 'overview'
            ),
            None,
        )
        if title_heading is None:
            continue
        text = clean_text(section)
        record = make_record(
            title_heading,
            date_heading,
            text,
            f'{CONCERTS_URL}#{section["id"]}',
            text,
        )
        if record:
            records.append(record)
    return records


def parse_archive_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.find('main')
    if main is None:
        return None
    title = main.find('h1')
    date_element = next(
        (element for element in main.find_all(['p', 'h2']) if DATE_RE.search(clean_text(element))),
        None,
    )
    if title is None or date_element is None:
        return None
    return make_record(title, date_element, clean_text(main), url, clean_text(main))


def sitemap_urls(xml):
    soup = BeautifulSoup(xml, 'xml')
    urls = []
    for loc in soup.find_all('loc'):
        url = clean_text(loc)
        if url and urlparse(url).netloc == urlparse(SOURCE_URL).netloc:
            urls.append(url)
    return urls


class ChamberMusicAbqComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chambermusicabq_com',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(CONCERTS_URL, timeout=60)
            response.raise_for_status()
            records = parse_concerts_page(response.text)

            sitemap_response = session.get(SITEMAP_URL, timeout=45)
            sitemap_response.raise_for_status()
            for url in sitemap_urls(sitemap_response.text):
                if url.rstrip('/') == CONCERTS_URL.rstrip('/'):
                    continue
                try:
                    archive_response = session.get(url, timeout=45)
                    archive_response.raise_for_status()
                    record = parse_archive_page(archive_response.text, url)
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Chamber Music Albuquerque archive page',
                        event='crawler_detail_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Chamber Music Albuquerque concert catalog',
                event='crawler_fetch_failed',
                level='error',
                url=CONCERTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        if not records:
            log_message(
                'No Chamber Music Albuquerque concerts found',
                event='crawler_empty_listing',
                level='warning',
                url=CONCERTS_URL,
                record_count=0,
            )
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    ChamberMusicAbqComCrawler().run()


if __name__ == '__main__':
    main()
