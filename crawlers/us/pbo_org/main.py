import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://pbo.org/'
SOURCE = 'Portland Baroque Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

SEASON_PATH_RE = re.compile(r'^/(\d{4})-\d{2}-season(?:/|$)')
OCCURRENCE_RE = re.compile(
    r'^([A-Z][a-z]{2})\.?\s+(\d{1,2}),\s+(\d{4})\s*\|\s*'
    r'(\d{1,2}(?::\d{2})?\s*[AP]M)$',
    re.IGNORECASE,
)
CITY_RE = re.compile(r'^(.+?),\s*[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?$')


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def current_season_urls(soup):
    """Return first-party links belonging to the newest advertised season."""
    links_by_year = {}
    for link in soup.select('a[href]'):
        url = urljoin(SOURCE_URL, link.get('href'))
        parsed = urlparse(url)
        if parsed.netloc not in {'pbo.org', 'www.pbo.org'}:
            continue
        match = SEASON_PATH_RE.match(parsed.path)
        if match:
            links_by_year.setdefault(int(match.group(1)), set()).add(url.split('#')[0])

    if not links_by_year:
        return []
    return sorted(links_by_year[max(links_by_year)])


def parse_occurrence(item):
    lines = [clean_text(value) for value in item.get_text('\n', strip=True).splitlines()]
    lines = [value for value in lines if value]
    if not lines:
        return None

    match = OCCURRENCE_RE.fullmatch(lines[0])
    if not match:
        return None
    month, day, year, event_time = match.groups()
    try:
        event_date = datetime.strptime(
            f'{month} {day} {year}', '%b %d %Y'
        ).date().isoformat()
        time_from = datetime.strptime(
            event_time.upper().replace(' ', ''), '%I:%M%p' if ':' in event_time else '%I%p'
        ).strftime('%H:%M')
    except ValueError:
        return None

    venue_node = item.select_one('a strong, strong')
    venue = clean_text(venue_node.get_text(' ', strip=True)) if venue_node else ''
    city = ''
    for line in lines[1:]:
        city_match = CITY_RE.fullmatch(line)
        if city_match:
            city = clean_text(city_match.group(1))

    if not venue or not city:
        return None
    return event_date, time_from, venue, city


def page_description(soup):
    article = soup.select_one('main article') or soup.select_one('main')
    if not article:
        return None
    article = BeautifulSoup(str(article), 'html.parser')
    for node in article.select(
        'nav, script, style, .nr-per-locations, .nr-nav-sub-menu, '
        '.nr-button, [class*="ticket"]'
    ):
        node.decompose()
    text = article.get_text('\n', strip=True)
    lines = []
    for line in text.splitlines():
        line = clean_text(line)
        if line and line not in lines:
            lines.append(line)
    return '\n'.join(lines) or None


def parse_event_page(soup, url):
    title_node = soup.select_one('main h1') or soup.select_one('h1')
    title = clean_text(title_node.get_text(' ', strip=True)) if title_node else ''
    if not title:
        return []

    description = page_description(soup)
    records = []
    for item in soup.select('.nr-per-locations li'):
        occurrence = parse_occurrence(item)
        if not occurrence:
            continue
        event_date, time_from, venue, city = occurrence
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


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    home_soup = get_soup(session, SOURCE_URL)
    seed_urls = current_season_urls(home_soup)

    # Season overview pages link every program, while the homepage may only show
    # the next few. Expand each seed once, then keep only pages with occurrences.
    event_urls = set(seed_urls)
    for url in seed_urls:
        soup = get_soup(session, url)
        event_urls.update(current_season_urls(soup))

    records = []
    for url in sorted(event_urls):
        try:
            records.extend(parse_event_page(get_soup(session, url), url))
        except requests.RequestException as error:
            log_message(
                'Could not fetch concert page',
                event='crawler_page_failed',
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
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )


class PboOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='pbo_org',
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
    PboOrgCrawler().run()


if __name__ == '__main__':
    main()
