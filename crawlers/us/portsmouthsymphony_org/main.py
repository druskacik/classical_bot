import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.portsmouthsymphony.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events/')
PAST_SEASONS_URL = urljoin(EVENTS_URL, 'past-seasons/')
SOURCE = 'Portsmouth Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

OCCURRENCE_RE = re.compile(
    r'(?P<date>(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
    r'[A-Z][a-z]+\s+\d{1,2},\s+\d{4})'
    r'(?:\s+(?P<time>\d{1,2}(?::\d{2})?\s*[ap]m))?',
    re.IGNORECASE,
)
ADDRESS_RE = re.compile(
    r'^(?P<venue>.*?)(?:,?\s+)\d+[A-Za-z]?(?:[-–]\d+)?\s+.*?,\s*'
    r'(?P<city>[A-Za-z][A-Za-z .\'-]+),\s*(?:NH|ME)\b',
    re.IGNORECASE,
)
CITY_STATE_RE = re.compile(r',\s*(?P<city>[A-Za-z][A-Za-z .\'-]+),\s*(?:NH|ME)\b', re.I)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def event_links(soup):
    links = set()
    for card in soup.select('.card.event'):
        link = card.find('a', href=True)
        if link:
            links.add(urljoin(SOURCE_URL, link['href']))
    return links


def discover_event_urls(session):
    current = get_soup(session, EVENTS_URL)
    urls = event_links(current)

    archive = get_soup(session, PAST_SEASONS_URL)
    season_urls = {
        urljoin(SOURCE_URL, link['href'])
        for link in archive.select('a[href*="/event/"][href]')
        if re.search(r'/event/20\d{2}-(?:20)?\d{2}/?$', link['href'])
    }
    for season_url in sorted(season_urls):
        season = get_soup(session, season_url)
        series_urls = {
            urljoin(SOURCE_URL, link['href'])
            for link in season.select('.card.series a[href], a[href*="/event/"][href]')
            if urljoin(SOURCE_URL, link['href']).rstrip('/') != season_url.rstrip('/')
        }
        for series_url in sorted(series_urls):
            urls.update(event_links(get_soup(session, series_url)))

    return sorted(urls)


def parse_date(value):
    try:
        return datetime.strptime(value, '%A, %B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    if not value:
        return None
    compact = re.sub(r'\s+', '', value).upper()
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(compact, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_location(value):
    location = clean_text(value).replace('\n', ' ').strip(' ,')
    match = ADDRESS_RE.search(location)
    if match:
        return match.group('venue').strip(' ,'), match.group('city').strip()

    city_match = CITY_STATE_RE.search(location)
    if not city_match:
        return None, None
    city = city_match.group('city').strip()
    before_city = location[:city_match.start()].strip(' ,')
    venue = re.split(r',?\s+\d+[A-Za-z]?(?:[-–]\d+)?\s+', before_city, maxsplit=1)[0].strip(' ,')
    return (venue or None), city


def parse_event_page(soup, url):
    title_node = soup.select_one('h1.article-h1')
    event_time = soup.select_one('.event-time')
    if not title_node or not event_time:
        return []

    title = clean_text(title_node)
    spans = event_time.select('span')
    occurrences = list(OCCURRENCE_RE.finditer(clean_text(spans[0]) if spans else ''))
    location = clean_text(spans[1]) if len(spans) > 1 else ''
    venue, city = parse_location(location)
    if not title or not occurrences or not venue or not city:
        return []

    body = soup.select_one('.article-body > div:first-child')
    description = clean_text(body) or None
    records = []
    for occurrence in occurrences:
        event_date = parse_date(occurrence.group('date'))
        if not event_date:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(occurrence.group('time')),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    urls = discover_event_urls(session)
    for url in urls:
        try:
            records.extend(parse_event_page(get_soup(session, url), url))
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
            'No parseable concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class PortsmouthSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='portsmouthsymphony_org',
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
    PortsmouthSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
