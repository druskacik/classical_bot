import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sacms.org/'
ARCHIVE_URL = urljoin(SOURCE_URL, 'past-concerts')
SOURCE = 'San Antonio Chamber Music Society'
CITY = 'San Antonio'
VENUE = 'Trinity Baptist Church'
TIME_FROM = '15:15'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_PATTERN = (
    r'(?:January|February|March|April|May|June|July|August|September|October|'
    r'November|December)\s+\d{1,2},\s+\d{4}'
)
DATE_RE = re.compile(DATE_PATTERN, re.IGNORECASE)
ARCHIVE_EVENT_RE = re.compile(
    rf'(?m)^([^\n]+)\n({DATE_PATTERN})\s*$',
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
    match = DATE_RE.search(value or '')
    if not match:
        return None
    try:
        return datetime.strptime(match.group(0), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def make_record(title, event_date, url, description, venue=VENUE, time_from=TIME_FROM):
    return {
        'title': clean_text(title),
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': clean_text(venue),
        'city': CITY,
        'country_code': 'US',
        'description': clean_text(description) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def current_detail_urls(home_soup):
    urls = []
    main = home_soup.select_one('main') or home_soup
    for link in main.select('a[href]'):
        url = urljoin(SOURCE_URL, link.get('href'))
        parsed = urlparse(url)
        if parsed.netloc != 'www.sacms.org' or parsed.path in {'', '/', '/past-concerts'}:
            continue
        url = f'{parsed.scheme}://{parsed.netloc}{parsed.path}'
        if url not in urls:
            urls.append(url)
    return urls


def parse_current_page(soup, url):
    main = soup.select_one('main')
    if not main:
        return None
    text = clean_text(main.get_text('\n', strip=True))
    event_date = parse_date(text)
    venue_match = re.search(r'Venue\s*\n([^\n]+)', text, re.IGNORECASE)
    if not event_date or not venue_match:
        return None

    title_node = main.select_one('h1, h2')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    if not title:
        return None

    time_match = re.search(r'Time:\s*(\d{1,2}(?::\d{2})?\s*[AP]M)', text, re.IGNORECASE)
    time_from = TIME_FROM
    if time_match:
        for pattern in ('%I:%M %p', '%I %p'):
            try:
                time_from = datetime.strptime(time_match.group(1).upper(), pattern).strftime('%H:%M')
                break
            except ValueError:
                pass

    return make_record(
        title,
        event_date,
        url,
        text,
        venue=venue_match.group(1),
        time_from=time_from,
    )


def parse_archive(soup):
    records = []
    for description in soup.select('[data-sqsp-accordion-block-item-description]'):
        text = clean_text(description.get_text('\n', strip=True))
        matches = list(ARCHIVE_EVENT_RE.finditer(text))
        for index, match in enumerate(matches):
            event_date = parse_date(match.group(2))
            title = clean_text(match.group(1))
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            body = clean_text(text[match.end():end])
            if title and event_date:
                records.append(make_record(title, event_date, ARCHIVE_URL, body))
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    home_response = session.get(SOURCE_URL, timeout=45)
    home_response.raise_for_status()
    home_soup = BeautifulSoup(home_response.text, 'html.parser')

    records = []
    for url in current_detail_urls(home_soup):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            record = parse_current_page(BeautifulSoup(response.text, 'html.parser'), url)
            if record:
                records.append(record)
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    archive_response = session.get(ARCHIVE_URL, timeout=45)
    archive_response.raise_for_status()
    records.extend(parse_archive(BeautifulSoup(archive_response.text, 'html.parser')))

    unique = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        unique[key] = record

    if not unique:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(unique.values(), key=lambda item: (item['date'], item['title'], item['url']))


class SacmsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sacms_org',
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
    SacmsOrgCrawler().run()


if __name__ == '__main__':
    main()
