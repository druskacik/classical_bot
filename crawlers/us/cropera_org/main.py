import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.cropera.org/'
SOURCE = 'Cedar Rapids Opera'
SITEMAP_URL = urljoin(SOURCE_URL, 'pages-sitemap.xml')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|'
    'November|December'
)
DATE_TIME_RE = re.compile(
    rf'\b({MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,)?\s+(\d{{4}})'
    r'(?:\s*\|\s*(\d{1,2}(?::\d{2})?\s*[AP]M))?',
    re.IGNORECASE,
)
SEASON_PATH_RE = re.compile(r'^/season-\d+/?$', re.IGNORECASE)

VENUE_CITIES = {
    "Iowa Children's Museum": 'Coralville',
    'The Iowa Children’s Museum': 'Coralville',
}

SKIP_LINES = {
    '< Previous Event',
    'Next Event >',
    'LOCATION:',
    'LEARN MORE',
    'TICKETS',
    'RESERVE YOUR SEAT',
    'SUBSCRIBE TO OUR NEWSLETTER',
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ').replace('\u200b', ' ')).strip()


def parse_date_time(value):
    match = DATE_TIME_RE.search(clean_text(value))
    if not match:
        return None
    month, day, year, event_time = match.groups()
    try:
        event_date = datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
    except ValueError:
        return None

    time_from = None
    if event_time:
        normalized = re.sub(r'\s+', ' ', event_time.upper())
        for pattern in ('%I:%M %p', '%I %p', '%I:%M%p', '%I%p'):
            try:
                time_from = datetime.strptime(normalized, pattern).strftime('%H:%M')
                break
            except ValueError:
                pass
    return event_date, time_from


def page_lines(main):
    return [clean_text(line) for line in main.get_text('\n', strip=True).splitlines() if clean_text(line)]


def detail_title(soup):
    if soup.title:
        title = clean_text(soup.title.get_text()).split('|', 1)[0].strip()
        if title:
            return title
    heading = soup.select_one('main h1, main h2')
    return clean_text(heading.get_text(' ', strip=True)) if heading else ''


def standard_occurrences(lines):
    dates = []
    location_index = None
    for index, line in enumerate(lines):
        parsed = parse_date_time(line)
        if parsed:
            dates.append(parsed)
        if line.upper() in {'LOCATION:', 'LOCATION'}:
            location_index = index
            break

    if location_index is None or location_index + 1 >= len(lines):
        return []
    venue = clean_text(lines[location_index + 1])
    if not venue or venue.lower() == 'online':
        return []
    return [(event_date, event_time, venue) for event_date, event_time in dates]


def touring_occurrences(lines):
    """Parse pages which pair each public performance date with its own venue."""
    occurrences = []
    for index, line in enumerate(lines[:-1]):
        parsed = parse_date_time(line)
        if not parsed:
            continue
        venue = clean_text(lines[index + 1])
        if (
            not venue
            or parse_date_time(venue)
            or venue.upper() in {'LOCATION:', 'PUBLIC PERFORMANCE DATES AND LOCATIONS:'}
        ):
            continue
        occurrences.append((*parsed, venue))
    return occurrences


def description_from_lines(lines, title):
    kept = []
    for line in lines:
        if line == title or line in SKIP_LINES or parse_date_time(line):
            continue
        upper = line.upper()
        if upper.startswith(('INDIVIDUAL TICKETS', 'GET TICKETS', 'PURCHASE TICKETS')):
            continue
        if upper.startswith('STAY IN THE LOOP'):
            break
        if line not in kept:
            kept.append(line)
    return '\n'.join(kept) or None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.find('main')
    if not main:
        return []
    title = detail_title(soup)
    lines = page_lines(main)
    is_touring_page = any('PUBLIC PERFORMANCE DATES AND LOCATIONS' in line.upper() for line in lines)
    occurrences = touring_occurrences(lines) if is_touring_page else standard_occurrences(lines)
    description = description_from_lines(lines, title)

    records = []
    for event_date, time_from, venue in occurrences:
        city = VENUE_CITIES.get(venue, 'Cedar Rapids')
        records.append({
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
        })
    return records


def season_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    sitemap = BeautifulSoup(response.content, 'xml')
    urls = []
    for node in sitemap.find_all('loc'):
        url = clean_text(node.get_text())
        if urlparse(url).netloc == urlparse(SOURCE_URL).netloc and SEASON_PATH_RE.match(urlparse(url).path):
            urls.append(url)
    return sorted(set(urls))


def event_urls_from_season(html):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.find('main')
    if not main:
        return []
    urls = []
    for link in main.select('a[href]'):
        url = urljoin(SOURCE_URL, link.get('href'))
        parsed = urlparse(url)
        if parsed.netloc != urlparse(SOURCE_URL).netloc:
            continue
        if parsed.path in {'', '/'} or SEASON_PATH_RE.match(parsed.path) or parsed.path.startswith('/_files/'):
            continue
        urls.append(url.split('#', 1)[0])
    return sorted(set(urls))


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    seasons = season_urls(session)
    event_urls = set()
    for season_url in seasons:
        response = session.get(season_url, timeout=45)
        response.raise_for_status()
        event_urls.update(event_urls_from_season(response.text))

    records = []
    for url in sorted(event_urls):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            records.extend(parse_detail(response.text, url))
        except requests.RequestException as error:
            log_message(
                'Event detail request failed',
                event='crawler_detail_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class CroperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cropera_org',
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
    CroperaOrgCrawler().run()


if __name__ == '__main__':
    main()
