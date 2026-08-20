import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wbopera.org/'
SOURCE = 'West Bay Opera'
CITY = 'Palo Alto'
COUNTRY_CODE = 'US'

HEADERS = {
    # Duda's edge rejects requests with a generic requests user agent.
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) HeadlessChrome/151.0.0.0 Safari/537.36'
    ),
    'sec-ch-ua': '"Chromium";v="151", "Not=A?Brand";v="99"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Linux"',
    'upgrade-insecure-requests': '1',
}

DATE_LINE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?'
    r'(?:,\s*(?P<year>\d{4}))?\s*[-–—]\s*'
    r'(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap])\.?m\.?.*$',
    re.IGNORECASE,
)
YEAR_IN_URL = re.compile(r'(?:19|20)\d{2}')


def clean_text(value):
    if not value:
        return ''
    text = str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def production_links(soup):
    links = {}
    excluded = re.compile(
        r'bios?|staff|materials|results|policy|scores|auditions|gallery|reviews?',
        re.IGNORECASE,
    )
    for anchor in soup.select('a[href]'):
        text = clean_text(anchor.get_text(' ', strip=True))
        url = urljoin(SOURCE_URL, anchor.get('href')).split('#', 1)[0]
        path = urlparse(url).path
        if urlparse(url).netloc != 'www.wbopera.org' or excluded.search(text + path):
            continue
        year = YEAR_IN_URL.search(text) or YEAR_IN_URL.search(path)
        # Production archive links carry a year. The three upcoming season
        # cards are the only production links whose visible text omits it.
        upcoming = path.lower() in {
            '/abduction-from-the-seraglio-2026',
            '/don-carlos-2027',
            '/turandot-2027',
        }
        if year or upcoming:
            resolved_year = int((year or YEAR_IN_URL.search(path)).group())
            title = re.sub(r'\s*[-–—]?\s*(?:19|20)\d{2}\s*$', '', text).strip()
            if not title:
                title = path.strip('/').rsplit('-', 1)[0].replace('-', ' ').title()
            # Image links repeat some productions with no useful anchor text.
            links.setdefault(url, (resolved_year, title))
    return links


def page_title(soup, fallback):
    headings = [clean_text(node.get_text(' ', strip=True)) for node in soup.select('h1, h2')]
    ignored = {'about', 'cast', 'chorus', 'orchestra', 'creative team'}
    for heading in headings:
        if heading and heading.casefold() not in ignored:
            return heading
    title = clean_text(soup.title.get_text(' ', strip=True)) if soup.title else fallback
    return re.sub(r'\s*[-–—]\s*(?:19|20)\d{2}\s*$', '', title).strip() or fallback


def parse_occurrence(line, default_year):
    match = DATE_LINE.match(line)
    if not match:
        return None
    year = int(match.group('year') or default_year)
    try:
        event_date = datetime.strptime(
            f"{match.group('month')} {match.group('day')} {year}", '%B %d %Y'
        ).date()
    except ValueError:
        try:
            event_date = datetime.strptime(
                f"{match.group('month')} {match.group('day')} {year}", '%b %d %Y'
            ).date()
        except ValueError:
            return None
    hour = int(match.group('hour')) % 12
    if match.group('ampm').lower() == 'p':
        hour += 12
    return event_date.isoformat(), f"{hour:02d}:{int(match.group('minute') or 0):02d}"


def production_records(soup, url, default_year, title):
    content = soup.select_one('#dm_content') or soup.body
    lines = [clean_text(line) for line in content.get_text('\n').splitlines()]
    lines = [line for line in lines if line]

    # The performance block is a run of weekday/date/time lines immediately
    # followed by the theatre. Later dates occur in histories and reviews.
    groups = []
    current = []
    for index, line in enumerate(lines):
        occurrence = parse_occurrence(line, default_year)
        if occurrence:
            current.append((index, occurrence))
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    group = next((item for item in groups if len(item) >= 2), None)
    if not group:
        return []
    following = ' '.join(lines[group[-1][0] + 1:group[-1][0] + 7])
    if 'Lucie Stern Theatre' not in following:
        return []

    # Drop the global navigation (which lists many unrelated operas) while
    # retaining the production synopsis, composer, cast, orchestra and reviews.
    description_lines = lines[max(0, group[0][0] - 15):]
    description = '\n'.join(description_lines)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': 'Lucie Stern Theatre',
            'city': CITY,
            'country_code': COUNTRY_CODE,
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for _, (event_date, time_from) in group
    ]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    home = get_soup(session, SOURCE_URL)
    records = []
    for url, (year, title) in production_links(home).items():
        try:
            records.extend(production_records(get_soup(session, url), url, year, title))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape opera production',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class WboperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wbopera_org',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    WboperaOrgCrawler().run()


if __name__ == '__main__':
    main()
