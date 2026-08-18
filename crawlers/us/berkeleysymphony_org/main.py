import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.berkeleysymphony.org/'
EVENT_SITEMAP_URL = f'{SOURCE_URL}event-sitemap.xml'
LISTING_URL = f'{SOURCE_URL}series-overview/'
SOURCE = 'Berkeley Symphony'

VENUE_CITIES = {
    'Henry J. Kaiser Center for the Arts': 'Oakland',
    'Littlefield Concert Hall': 'Moraga',
    'The Hillside Club': 'Berkeley',
    'Zellerbach Hall, UC Berkeley': 'Berkeley',
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\.?[,]?\s*'
    r'([A-Za-z]{3,9})\s+(\d{1,2}),\s+(\d{4})\s+at\s+'
    r'(\d{1,2})(?::(\d{2}))?\s*([AP]M)',
    re.IGNORECASE,
)
CITY_RE = re.compile(r',\s*([^,]+),\s*CA(?:\s+\d{5}(?:-\d{4})?)?\s*$', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_urls(session):
    response = session.get(EVENT_SITEMAP_URL, timeout=45)
    response.raise_for_status()
    sitemap = BeautifulSoup(response.text, 'xml')
    urls = []
    for node in sitemap.find_all('loc'):
        url = clean_text(node.get_text())
        if re.search(r'/event/[^/]+/?$', url):
            urls.append(url)
    return list(dict.fromkeys(urls))


def parse_occurrences(value):
    occurrences = []
    for match in DATE_TIME_RE.finditer(clean_text(value)):
        month, day, year, hour, minute, meridiem = match.groups()
        try:
            parsed = datetime.strptime(
                f'{month} {day} {year} {hour}:{minute or "00"} {meridiem}',
                '%b %d %Y %I:%M %p',
            )
        except ValueError:
            continue
        occurrences.append((parsed.date().isoformat(), parsed.strftime('%H:%M')))
    return occurrences


def parse_city(address):
    match = CITY_RE.search(clean_text(address))
    return clean_text(match.group(1)) if match else ''


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title_node = soup.select_one('.event-title')
    date_node = soup.select_one('.event-date-time')
    venue_node = soup.select_one('.event-venue')
    address_node = soup.select_one('.event-venue-address')

    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    document_title = clean_text(soup.title.get_text(' ', strip=True) if soup.title else '')
    expected_title = re.sub(r'\s*[-|]\s*Berkeley Symphony.*$', '', document_title).strip()
    # The site currently returns the newest event body for a number of old
    # URLs. Never attach that occurrence to a stale sitemap URL.
    if expected_title.casefold() != title.casefold():
        return []
    venue = clean_text(venue_node.get_text(' ', strip=True) if venue_node else '')
    address = clean_text(address_node.get_text(' ', strip=True) if address_node else '')
    city = parse_city(address)
    occurrences = parse_occurrences(date_node.get_text(' ', strip=True) if date_node else '')
    if not title or not venue or not city or not occurrences:
        return []

    description_parts = []
    for selector in ('.event-description', '.event-musical-program'):
        node = soup.select_one(selector)
        text = clean_text(node.get_text('\n', strip=True) if node else '')
        if text and text not in description_parts:
            description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from in occurrences
    ]


def parse_listing(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for card in soup.select('a.event[href*="/event/"]'):
        title_node = card.select_one('.event-title')
        date_node = card.select_one('.event-date-time')
        venue_node = card.select_one('.event-venue')
        title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
        venue = clean_text(venue_node.get_text(' ', strip=True) if venue_node else '')
        city = VENUE_CITIES.get(venue, '')
        url = clean_text(card.get('href'))
        occurrences = parse_occurrences(date_node.get_text(' ', strip=True) if date_node else '')
        if not title or not venue or not city or not url or not occurrences:
            continue
        for event_date, time_from in occurrences:
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def scrape_concerts(session=None):
    # This host can incorrectly reuse the first event response on a persistent
    # connection, so production detail requests deliberately use fresh ones.
    sitemap_session = session or requests.Session()
    sitemap_session.headers.update(HEADERS)
    listing_response = sitemap_session.get(LISTING_URL, timeout=45)
    listing_response.raise_for_status()
    records = parse_listing(listing_response.text)
    listing_urls = {record['url'].rstrip('/') for record in records}
    urls = event_urls(sitemap_session)

    for url in urls:
        if url.rstrip('/') in listing_urls:
            continue
        try:
            if session is None:
                response = requests.get(url, headers=HEADERS, timeout=45)
            else:
                response = session.get(url, timeout=45)
            response.raise_for_status()
            records.extend(parse_event_page(response.text, response.url))
        except requests.RequestException as error:
            log_message(
                'Event page request failed',
                event='crawler_event_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No parseable event occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENT_SITEMAP_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class BerkeleySymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='berkeleysymphony_org',
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
    BerkeleySymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
