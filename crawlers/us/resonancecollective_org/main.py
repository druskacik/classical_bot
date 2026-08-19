import re
from datetime import date, datetime
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.resonancecollective.org/'
SOURCE = 'Resonance Collective'
LISTING_URLS = (
    urljoin(SOURCE_URL, 'upcoming-events'),
    urljoin(SOURCE_URL, 'concert-archive'),
)
TIME_ZONE = ZoneInfo('America/Los_Angeles')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?'
    r'(?:,?\s+(?P<year>20\d{2}))?\b',
    re.IGNORECASE,
)
DATE_RE_MONTH_FIRST = re.compile(
    r'\b(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(?P<year>20\d{2}))?\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r'\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>[ap])\.?m\.?(?:\s*[-–]|\b)',
    re.IGNORECASE,
)
ADDRESS_RE = re.compile(
    r'\b(?:Street|St\.?|Avenue|Ave\.?|Boulevard|Blvd\.?|Road|Rd\.?|Drive|Dr\.?|Way)\s+'
    r'(?P<city>[A-Za-z][A-Za-z .\'-]+?),\s*(?:CA|California)\s+\d{5}(?:-\d{4})?\b',
    re.IGNORECASE,
)


def clean_lines(element):
    if element is None:
        return []
    text = element.get_text('\n', strip=True).replace('\xa0', ' ').replace('\u200b', '')
    return [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines() if line.strip()]


def event_links(session):
    links = {}
    for listing_url in LISTING_URLS:
        response = session.get(listing_url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        for anchor in soup.select('main a[href]'):
            if not anchor.find('img'):
                continue
            url = urljoin(SOURCE_URL, anchor['href'])
            parsed = urlsplit(url)
            if parsed.netloc.lower() == 'www.resonancecollective.org':
                links[parsed._replace(query='', fragment='').geturl()] = listing_url
    return links


def parse_date(lines, listing_url, today=None):
    today = today or datetime.now(TIME_ZONE).date()
    for line in lines:
        match = DATE_RE.search(line) or DATE_RE_MONTH_FIRST.search(line)
        if not match:
            continue
        year_text = match.group('year')
        if year_text:
            year = int(year_text)
        elif listing_url.endswith('/upcoming-events'):
            year = today.year
        else:
            continue
        try:
            parsed = datetime.strptime(
                f"{match.group('month')} {match.group('day')} {year}", '%B %d %Y'
            ).date()
        except ValueError:
            continue
        if not year_text and parsed < today and (today - parsed).days > 183:
            parsed = date(year + 1, parsed.month, parsed.day)
        return parsed.isoformat(), line
    return None, None


def parse_time(date_line, lines):
    candidates = [date_line] + lines[lines.index(date_line) + 1:lines.index(date_line) + 4]
    for line in candidates:
        match = TIME_RE.search(line or '')
        if not match:
            continue
        hour = int(match.group('hour')) % 12
        if match.group('meridiem').lower() == 'p':
            hour += 12
        return f"{hour:02d}:{int(match.group('minute') or 0):02d}"
    return None


def parse_location(lines, date_line):
    date_index = lines.index(date_line)
    for index in range(date_index - 1, 0, -1):
        address = ADDRESS_RE.search(lines[index])
        if not address:
            continue
        city = re.sub(r'\s+', ' ', address.group('city')).strip()
        venue = lines[index - 1].strip() if index > 1 else ''
        if venue and not re.search(r'\bwith\b|\bpresented by\b', venue, re.IGNORECASE):
            return venue, city
    return None, None


def page_to_record(html, url, listing_url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    lines = clean_lines(main)
    heading = main.find(['h1', 'h2']) if main else None
    title = re.sub(r'\s+', ' ', heading.get_text(' ', strip=True)).strip() if heading else ''
    event_date, date_line = parse_date(lines, listing_url)
    if not title or not event_date or not date_line:
        return None
    venue, city = parse_location(lines, date_line)
    if not venue or not city:
        return None

    description_lines = [line for line in lines if line != title]
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(date_line, lines),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': '\n'.join(description_lines) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    links = event_links(session)
    records = []
    for url, listing_url in links.items():
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            record = page_to_record(response.text, url, listing_url)
            if record:
                records.append(record)
        except requests.RequestException as error:
            log_message(
                'Could not fetch event detail',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    if not records:
        log_message(
            'No parseable event occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class ResonanceCollectiveOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='resonancecollective_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ResonanceCollectiveOrgCrawler().run()


if __name__ == '__main__':
    main()
