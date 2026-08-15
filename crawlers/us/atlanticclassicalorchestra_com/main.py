import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://atlanticclassicalorchestra.com/'
SOURCE = 'Atlantic Classical Orchestra'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DETAIL_PATH_RE = re.compile(r'^/(?:masterworks-[ivx]+|chamber-[ivx]+|holiday-concert)/$')
TIME_RE = re.compile(r'(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap])\.?m\.?', re.I)

VENUE_CITIES = {
    'Blake Library': 'Stuart',
    'Community Church of Vero Beach': 'Vero Beach',
    'Redeemer Lutheran Church': 'Stuart',
    'St. Edward’s, Waxlax Center': 'Vero Beach',
    'The Lyric Theatre': 'Stuart',
    'Vero Beach Museum of Art': 'Vero Beach',
}


def clean_text(value, separator=' '):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text(separator, strip=True)
    else:
        text = str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    response.encoding = 'utf-8'
    return BeautifulSoup(response.text, 'html.parser')


def detail_links(soup, page_url):
    links = set()
    for link in soup.select('a[href]'):
        url = urljoin(page_url, link.get('href', '')).split('#', 1)[0]
        parsed = urlparse(url)
        if parsed.netloc == urlparse(SOURCE_URL).netloc and DETAIL_PATH_RE.fullmatch(parsed.path):
            links.add(url)
    return links


def discover_detail_urls(session):
    concerts_soup = get_soup(session, CONCERTS_URL)
    detail_urls = detail_links(concerts_soup, CONCERTS_URL)

    # The first page links to named series overview pages. Following those
    # pages keeps newly published programme pages discoverable without relying
    # on season-specific titles or years.
    overview_urls = set()
    for link in concerts_soup.select('a[href]'):
        url = urljoin(CONCERTS_URL, link.get('href', '')).split('#', 1)[0]
        if urlparse(url).path in ('/masterworks-series/', '/chamber-series/'):
            overview_urls.add(url)
    for url in overview_urls:
        detail_urls.update(detail_links(get_soup(session, url), url))
    return sorted(detail_urls)


def parse_date(value):
    value = clean_text(value)
    for date_format in ('%A, %B %d, %Y', '%B %d, %Y'):
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            pass
    return None


def parse_time(value):
    match = TIME_RE.fullmatch(clean_text(value))
    if not match:
        return None
    hour = int(match.group('hour')) % 12
    if match.group('ampm').lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group("minute") or 0):02d}'


def venue_and_city(value):
    value = clean_text(value).strip(' ,')
    for city in ('Vero Beach', 'Stuart'):
        suffix = f', {city}'
        if value.endswith(suffix):
            venue = value[:-len(suffix)].strip(' ,')
            return (venue, city) if venue else (None, None)
    city = VENUE_CITIES.get(value)
    return (value, city) if city else (None, None)


def description_text(soup):
    content = soup.select_one('[data-elementor-type="wp-page"]')
    if not content:
        return None
    # Ticket controls are operational copy, while the surrounding content
    # retains the synopsis, complete repertoire, artists, and duration.
    for element in content.select('a, script, style'):
        element.decompose()
    return clean_text(content, separator='\n') or None


def parse_detail(soup, url):
    title = clean_text(soup.select_one('[data-elementor-type="wp-page"] h1'))
    if title == 'Chamber Series':
        title = clean_text(soup.select_one('[data-elementor-type="wp-page"] h6'))
    description = description_text(soup)
    if not title:
        return []

    records = []
    for performance in soup.select('[data-elementor-type="wp-page"] .aco-perf'):
        event_date = parse_date(performance.select_one('.aco-perf__date'))
        venue, city = venue_and_city(performance.select_one('.aco-perf__venue'))
        time_text = clean_text(performance.select_one('.aco-perf__time'))
        times = [parse_time(match.group(0)) for match in TIME_RE.finditer(time_text)]
        times = [value for value in times if value]
        if not event_date or not venue or not city:
            log_message(
                'Skipping Atlantic Classical Orchestra occurrence with incomplete location or date',
                event='crawler_event_skipped',
                level='warning',
                url=url,
            )
            continue
        for time_from in times or [None]:
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
            })
    return records


class AtlanticClassicalOrchestraComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='atlanticclassicalorchestra_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            urls = discover_detail_urls(session)
            records = []
            for url in urls:
                records.extend(parse_detail(get_soup(session, url), url))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Atlantic Classical Orchestra concert pages',
                event='crawler_fetch_failed',
                level='error',
                url=CONCERTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        log_message(
            'Atlantic Classical Orchestra concerts parsed',
            event='crawler_records_parsed',
            record_count=len(records),
            url=CONCERTS_URL,
        )
        return records


def main():
    AtlanticClassicalOrchestraComCrawler().run()


if __name__ == '__main__':
    main()
