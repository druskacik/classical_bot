import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.symphonysanjose.org/'
SOURCE = 'Symphony San Jose'
SITEMAP_URL = urljoin(SOURCE_URL, 'page-sitemap.xml')
CITY = 'San Jose'
COUNTRY_CODE = 'US'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

SEASON_RE = re.compile(r'/attend/\d{4}-\d{4}-season/')
LISTING_RE = re.compile(
    r'/attend/\d{4}-\d{4}-season/(?:concerts|concerts-\d{4}-\d{4})/$'
)
DETAIL_RE = re.compile(r'/attend/\d{4}-\d{4}-season/(?:concerts|ballet)/[^/]+/$')
DATE_LINE_RE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
    r'([A-Za-z]+\s+\d{1,2},\s+\d{4})'
    r'(?:\s+at\s+(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?))?$',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        return datetime.strptime(value, '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    if not value:
        return None
    normalized = value.lower().replace('.', '').replace(' ', '')
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    urls = {
        clean_text(node.get_text())
        for node in soup.select('loc')
        if LISTING_RE.search(clean_text(node.get_text()))
    }
    return sorted(urls)


def detail_events(session):
    events = {}
    listings = listing_urls(session)
    for listing_url in listings:
        try:
            soup = get_soup(session, listing_url)
        except requests.RequestException as error:
            log_message(
                'Concert listing request failed',
                event='crawler_listing_failed',
                level='warning',
                url=listing_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        for link in soup.select('a[href]'):
            url = urljoin(listing_url, link.get('href'))
            if SEASON_RE.search(url) and DETAIL_RE.search(url):
                container = link.find_parent(
                    'div', class_='fusion-builder-row-inner'
                ) or link.parent
                dates = []
                if container:
                    for match in re.finditer(
                        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
                        r'([A-Za-z]+\s+\d{1,2},\s+\d{4})',
                        clean_text(container.get_text(' ', strip=True)),
                        re.IGNORECASE,
                    ):
                        parsed = parse_date(match.group(1))
                        if parsed and parsed not in dates:
                            dates.append(parsed)
                events.setdefault(url, set()).update(dates)
    return [(url, sorted(dates)) for url, dates in sorted(events.items())]


def parse_location(lines, last_date_index):
    for index in range(last_date_index + 1, min(last_date_index + 9, len(lines) - 1)):
        venue = lines[index].rstrip(',').strip()
        city_line = lines[index + 1]
        city_match = re.fullmatch(r'([^,]+),\s*CA(?:\s+\d{5})?', city_line, re.I)
        if city_match and venue:
            return venue, clean_text(city_match.group(1))
    return None, None


def parse_detail(soup, url, fallback_dates=None):
    content = soup.select_one('main') or soup.body
    if not content:
        return []
    lines = [clean_text(line) for line in content.get_text('\n', strip=True).splitlines()]
    lines = [line for line in lines if line]

    title_node = content.select_one('h1')
    title = clean_text(title_node.get_text(' ', strip=True)) if title_node else ''
    occurrences = []
    last_date_index = -1
    for index, line in enumerate(lines):
        match = DATE_LINE_RE.fullmatch(line)
        if not match:
            continue
        event_date = parse_date(match.group(1))
        if event_date:
            occurrences.append((event_date, parse_time(match.group(2))))
            last_date_index = index

    if not occurrences:
        occurrences = [(event_date, None) for event_date in (fallback_dates or [])]

    venue, city = parse_location(lines, last_date_index)
    if not title or not occurrences or not venue or not city:
        log_message(
            'Skipping incomplete concert detail',
            event='crawler_detail_skipped',
            level='warning',
            url=url,
            has_title=bool(title),
            occurrence_count=len(occurrences),
            has_venue=bool(venue),
            has_city=bool(city),
        )
        return []

    description = clean_text(content.get_text('\n', strip=True)) or None
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': COUNTRY_CODE,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from in occurrences
    ]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    events = detail_events(session)
    records = []
    for url, fallback_dates in events:
        try:
            records.extend(parse_detail(get_soup(session, url), url, fallback_dates))
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class SymphonySanJoseOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='symphonysanjose_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
    SymphonySanJoseOrgCrawler().run()


if __name__ == '__main__':
    main()
