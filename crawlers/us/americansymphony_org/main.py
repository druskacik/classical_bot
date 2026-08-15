import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://americansymphony.org/'
CURRENT_URL = urljoin(SOURCE_URL, 'current-season/')
ARCHIVE_URL = urljoin(SOURCE_URL, 'past-performances/')
SOURCE = 'American Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# Event pages publish a venue name but not its city. These mappings cover every
# venue in the live current-season and paginated past-performance feeds.
VENUE_CITIES = {
    'Alice Tully Hall, Lincoln Center': 'New York',
    'Bronx Music Hall': 'New York',
    'Brooklyn Bridge Park': 'Brooklyn',
    'Bryant Park': 'New York',
    'Carnegie Hall': 'New York',
    'Cathedral of St. John the Divine': 'New York',
    'Church of St. Monica': 'New York',
    'City Winery Grand Central': 'New York',
    'David Geffen Hall, Lincoln Center': 'New York',
    'Fisher Center, Sosnoff Theater': 'Annandale-on-Hudson',
    'Herald Square': 'New York',
    'Jazz at Lincoln Center': 'New York',
    'Kupferberg Center for the Arts': 'New York',
    'Louis Armstrong House Garden': 'New York',
    'Madison Square Garden': 'New York',
    'Magazzino Italian Art': 'Cold Spring',
    'Minor Memorial Library': 'Roxbury',
    'Morris Museum': 'Morristown',
    'Opus 40': 'Saugerties',
    'Perelman Performing Arts Center': 'New York',
    'Peter Norton Symphony Space': 'New York',
    'Radio City Music Hall': 'New York',
    "St. Bartholomew's Church": 'New York',
    'Statue of Liberty': 'New York',
    'The Riverside Church': 'New York',
    'Washington Lake Park Amphitheater': 'Sewell',
}

MONTH_RE = (
    r'January|February|March|April|May|June|July|August|September|October|'
    r'November|December'
)
SINGLE_DATE_RE = re.compile(
    rf'^({MONTH_RE})\s+(\d{{1,2}}),\s*(20\d{{2}})$', re.IGNORECASE,
)
SAME_MONTH_DATES_RE = re.compile(
    rf'^({MONTH_RE})\s+((?:\d{{1,2}}\s*(?:,|&)\s*)+\d{{1,2}}),\s*(20\d{{2}})$',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'^(\d{1,2})(?::(\d{2}))?\s*([AP])\.?M\.?$', re.IGNORECASE)


def clean_text(value, separator=' '):
    if not value:
        return ''
    text = value.get_text(separator, strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    response.encoding = 'utf-8'
    return response


def parse_dates(value):
    """Return explicit occurrence dates; do not expand run/overview ranges."""
    value = clean_text(value).replace('–', '-').replace('—', '-')
    if '-' in value:
        return []
    match = SINGLE_DATE_RE.fullmatch(value)
    if match:
        values = [(match.group(1), match.group(2), match.group(3))]
    else:
        match = SAME_MONTH_DATES_RE.fullmatch(value)
        if not match:
            return []
        values = [
            (match.group(1), day, match.group(3))
            for day in re.findall(r'\d{1,2}', match.group(2))
        ]
    dates = []
    for month, day, year in values:
        try:
            dates.append(datetime.strptime(f'{month} {day}, {year}', '%B %d, %Y').date().isoformat())
        except ValueError:
            return []
    return dates


def parse_time(value):
    match = TIME_RE.fullmatch(clean_text(value))
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).upper() == 'P' else 0)
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def icon_value(soup, class_name):
    icon = soup.select_one(f'.fl-module-icon i.{class_name}')
    if not icon:
        return ''
    wrapper = icon.find_parent(class_='fl-icon-wrap')
    text = wrapper.select_one('.fl-icon-text') if wrapper else None
    return clean_text(text)


def event_content(soup):
    candidates = []
    for builder in soup.select('.fl-builder-content'):
        if builder.select_one('.ua-icon-calendar2'):
            candidates.append(builder)
    return max(candidates, key=lambda node: len(clean_text(node)), default=None)


def records_from_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    content = event_content(soup)
    if not content:
        return []

    title_node = content.select_one('.pp-heading .pp-primary-title')
    title = clean_text(title_node)
    date_text = icon_value(content, 'ua-icon-calendar2')
    venue = icon_value(content, 'ua-icon-location-pin')
    city = VENUE_CITIES.get(venue)
    dates = parse_dates(date_text)
    if not title or not dates or not venue or not city:
        log_message(
            'Skipping ASO page without an explicit occurrence date or mapped venue',
            event='crawler_record_skipped', level='warning', url=url,
        )
        return []

    time_from = parse_time(icon_value(content, 'ua-icon-clock2'))
    description = clean_text(content, separator='\n') or None
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
        for event_date in dates
    ]


def detail_links(soup, base_url):
    links = set()
    for anchor in soup.select('a[href]'):
        url = urljoin(base_url, anchor.get('href', '')).split('#', 1)[0]
        parsed = urlparse(url)
        if parsed.netloc == 'americansymphony.org' and '/concerts/' in parsed.path:
            links.add(url)
    return links


def archive_pages(session):
    # The live archive currently has 11 pages. The cap prevents a broken
    # pagination redirect from turning a production run into an infinite loop.
    for page_number in range(1, 51):
        url = ARCHIVE_URL if page_number == 1 else urljoin(ARCHIVE_URL, f'page/{page_number}/')
        soup = BeautifulSoup(get_response(session, url).text, 'html.parser')
        # The page contains duplicate desktop/mobile grids. Post ids make the
        # count independent of responsive markup.
        post_ids = {
            node.get('data-id') for node in soup.select('.pp-content-post[data-id]')
            if node.get('data-id')
        }
        if not post_ids:
            break
        yield url, soup


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    links = set()
    current_soup = BeautifulSoup(get_response(session, CURRENT_URL).text, 'html.parser')
    links.update(detail_links(current_soup, CURRENT_URL))
    for page_url, soup in archive_pages(session):
        links.update(detail_links(soup, page_url))

    records = []
    for url in sorted(links):
        try:
            response = get_response(session, url)
            records.extend(records_from_detail(response.text, response.url))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch ASO concert detail', event='crawler_page_failed',
                level='warning', url=url, error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue'], record['url']): record
        for record in records
    }
    result = sorted(unique.values(), key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue'],
    ))
    if not result:
        log_message(
            'No valid ASO concert candidates found', event='crawler_empty_listing',
            level='warning', url=SOURCE_URL, record_count=0,
        )
    return result


class AmericanSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='americansymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'url'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    AmericanSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
