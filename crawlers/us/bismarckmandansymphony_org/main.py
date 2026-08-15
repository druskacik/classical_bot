import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://bismarckmandansymphony.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events/')
SOURCE = 'Bismarck Mandan Symphony Orchestra'
CITY = 'Bismarck'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_LINE_RE = re.compile(
    r'(?P<month>[A-Za-z]+)\s+(?P<day_one>\d{1,2})'
    r'(?:\s*&\s*(?P<day_two>\d{1,2}))?,\s*(?P<year>\d{4})'
    r'(?:,?\s*(?P<time>\d{1,2}(?::\d{2})?\s*[ap]m))?'
    r'(?:\s+at\s+(?:the\s+)?(?P<venue>[^\n]+))?',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    if not value:
        return None
    compact = re.sub(r'\s+', ' ', value.strip().upper())
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(compact, pattern).strftime('%H:%M')
        except ValueError:
            continue
    return None


def parse_event_heading(value):
    match = DATE_LINE_RE.search(clean_text(value))
    if not match:
        return [], None, ''

    dates = []
    for day in (match.group('day_one'), match.group('day_two')):
        if not day:
            continue
        try:
            parsed = datetime.strptime(
                f"{match.group('month')} {day} {match.group('year')}",
                '%B %d %Y',
            ).date().isoformat()
        except ValueError:
            continue
        dates.append(parsed)

    venue = clean_text(match.group('venue')).rstrip(' .')
    return dates, parse_time(match.group('time')), venue


def event_links(soup):
    links = []
    seen = set()
    events_path = urlparse(EVENTS_URL).path.rstrip('/') + '/'
    for anchor in soup.select('a[href]'):
        url = urljoin(EVENTS_URL, anchor.get('href', ''))
        parsed = urlparse(url)
        if parsed.netloc != urlparse(SOURCE_URL).netloc:
            continue
        path = parsed.path.rstrip('/') + '/'
        if not path.startswith(events_path) or path == events_path or url in seen:
            continue
        seen.add(url)
        links.append(url)
    return links


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    content = soup.select_one('#subpageContent')
    title_node = soup.select_one('h1.page-title')
    if not content or not title_node:
        return []

    title = clean_text(title_node.get_text(' ', strip=True))
    sections = [
        clean_text(node.get_text('\n', strip=True))
        for node in content.select('.wysiwyg')
    ]
    sections = [section for section in sections if section]
    if not title or not sections:
        return []

    dates, time_from, venue = parse_event_heading(sections[0])
    if not venue and 'MDU Resources' in sections[0]:
        venue = 'MDU Resources Community Bowl'
    if not dates or not venue:
        return []

    description = '\n\n'.join(sections)
    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for event_date in dates]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(EVENTS_URL, timeout=45)
    response.raise_for_status()

    links = event_links(BeautifulSoup(response.text, 'html.parser'))
    records = []
    for url in links:
        try:
            detail = session.get(url, timeout=45)
            detail.raise_for_status()
            records.extend(parse_event_page(detail.text, url))
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
            'No parseable concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


class BismarckMandanSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bismarckmandansymphony_org',
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
    BismarckMandanSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
