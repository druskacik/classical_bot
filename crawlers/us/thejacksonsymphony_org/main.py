import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.thejacksonsymphony.org/'
SOURCE = 'The Jackson Symphony'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
CITY = 'Jackson'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?P<date>[A-Z][a-z]+\s+\d{1,2},\s+\d{4})'
    r'(?:,\s*(?P<time>\d{1,2}(?::\d{2})?\s*[AP]M))?',
    re.IGNORECASE,
)

IGNORED_LINES = {
    '< back',
    'back',
    'buy tickets',
    'free concert',
    'learn more',
    'more info',
    'purchase tickets',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, parser='html.parser'):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, parser)


def event_urls(session):
    sitemap = get_soup(session, SITEMAP_URL, 'xml')
    child_urls = [node.get_text(strip=True) for node in sitemap.find_all('loc')]
    event_sitemaps = [
        url for url in child_urls
        if 'dynamic-' in url and (
            'eventsdataset' in url.lower() or 'season' in url.lower()
        )
    ]

    urls = []
    for sitemap_url in event_sitemaps:
        try:
            child = get_soup(session, sitemap_url, 'xml')
        except requests.RequestException as error:
            log_message(
                'Event sitemap request failed',
                event='crawler_sitemap_failed',
                level='warning',
                url=sitemap_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        urls.extend(node.get_text(strip=True) for node in child.find_all('loc'))

    return list(dict.fromkeys(urls))


def parse_time(value):
    if not value:
        return None
    normalized = re.sub(r'\s+', ' ', value.strip().upper())
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def plausible_venue(line, title):
    lowered = line.lower()
    return bool(
        line
        and line != title
        and lowered not in IGNORED_LINES
        and not DATE_TIME_RE.search(line)
        and len(line) <= 120
        and not line.endswith(('.', '!', '?'))
    )


def parse_event_page(soup, url):
    main = soup.find('main')
    if not main:
        return None

    lines = [clean_text(line) for line in main.get_text('\n', strip=True).splitlines()]
    lines = [line for line in lines if line]
    title_tag = soup.find('title')
    title = clean_text(title_tag.get_text(' ', strip=True) if title_tag else '')
    title = re.sub(r'\s*\|\s*The Jackson Symphony\s*$', '', title).strip()
    if not title:
        return None

    date_index = None
    date_match = None
    for index, line in enumerate(lines):
        match = DATE_TIME_RE.search(line)
        if match:
            date_index = index
            date_match = match
            break
    if date_index is None or date_match is None:
        return None

    try:
        event_date = datetime.strptime(
            date_match.group('date'), '%B %d, %Y'
        ).date().isoformat()
    except ValueError:
        return None

    venue = ''
    for neighbor in (date_index - 1, date_index + 1):
        if 0 <= neighbor < len(lines) and plausible_venue(lines[neighbor], title):
            venue = lines[neighbor]
            break
    if not venue:
        return None

    excluded = {title, lines[date_index], venue}
    description_lines = [
        line for line in lines
        if line not in excluded and line.lower() not in IGNORED_LINES
    ]
    description = clean_text('\n\n'.join(description_lines)) or None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(date_match.group('time')),
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []

    for url in urls:
        try:
            soup = get_soup(session, url)
            record = parse_event_page(soup, url)
        except requests.RequestException as error:
            log_message(
                'Event page request failed',
                event='crawler_event_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)

    unique = {}
    for record in records:
        key = (
            record['title'], record['date'], record['time_from'], record['venue']
        )
        current = unique.get(key)
        if current is None or len(record.get('description') or '') > len(current.get('description') or ''):
            unique[key] = record

    if not unique:
        log_message(
            'No concert detail pages could be parsed',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )

    return sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class TheJacksonSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='thejacksonsymphony_org',
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
    TheJacksonSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
