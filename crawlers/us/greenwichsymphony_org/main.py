import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.greenwichsymphony.org/'
SOURCE = 'Greenwich Symphony Orchestra'
VENUE = 'Performing Arts Center at Greenwich High School'
CITY = 'Greenwich'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RANGE_RE = re.compile(
    r'(?P<month>[A-Za-z]+)\s+(?P<day_one>\d{1,2})\s*&\s*'
    r'(?:(?P<month_two>[A-Za-z]+)\s+)?(?P<day_two>\d{1,2}),\s*'
    r'(?P<year>\d{4})'
)
TIME_RE = re.compile(
    r'(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)'
    r'\s+at\s+(?P<time>\d{1,2}(?::\d{2})?\s*[ap]m)',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_time(value):
    value = clean_text(value).replace('.', '').upper()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_dates(value):
    match = DATE_RANGE_RE.search(clean_text(value))
    if not match:
        return []

    parts = match.groupdict()
    values = [
        (parts['month'], parts['day_one']),
        (parts['month_two'] or parts['month'], parts['day_two']),
    ]
    dates = []
    for month, day in values:
        try:
            dates.append(
                datetime.strptime(
                    f"{month} {day} {parts['year']}", '%B %d %Y'
                ).date()
            )
        except ValueError:
            return []
    return dates


def program_links(html):
    soup = BeautifulSoup(html, 'html.parser')
    links = []
    for link in soup.find_all('a', href=True):
        if clean_text(link.get_text(' ', strip=True)).lower() != 'program':
            continue
        url = urljoin(SOURCE_URL, link['href'])
        if urlparse(url).netloc == urlparse(SOURCE_URL).netloc and url not in links:
            links.append(url)
    return links


def parse_program(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.find('main') or soup
    heading = main.find(['h1', 'h2'])
    heading_text = clean_text(heading.get_text(' ', strip=True)) if heading else ''
    dates = parse_dates(heading_text)
    if not dates:
        return []

    body_text = clean_text(main.get_text('\n', strip=True))
    venue = VENUE if VENUE.lower() in body_text.lower() else ''
    city = CITY if re.search(r'Greenwich\s*,?\s*CT', body_text, re.I) else ''
    if not venue or not city:
        return []

    times = {
        match.group('weekday').lower(): parse_time(match.group('time'))
        for match in TIME_RE.finditer(body_text)
    }
    description = body_text or None
    title = f'{SOURCE}: {heading_text}'

    records = []
    for event_date in dates:
        records.append({
            'title': title,
            'date': event_date.isoformat(),
            'url': url,
            'time_from': times.get(event_date.strftime('%A').lower()),
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

    response = session.get(SOURCE_URL, timeout=45)
    response.raise_for_status()
    links = program_links(response.text)
    records = []
    for url in links:
        try:
            detail_response = session.get(url, timeout=45)
            detail_response.raise_for_status()
            records.extend(parse_program(detail_response.text, url))
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
            'No concert program records found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['url']))


class GreenwichSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='greenwichsymphony_org',
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
        dedupe_subset=['date', 'time_from', 'venue', 'url'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    GreenwichSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
