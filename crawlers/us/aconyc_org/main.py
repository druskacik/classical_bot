import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://aconyc.org/'
ARCHIVE_URL = urljoin(SOURCE_URL, 'past-concerts/')
SOURCE = 'American Classical Orchestra'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}
DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|'
    r'Aug(?:ust)?|Sept(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
    r'\.?\s+\d{1,2},\s+\d{4})\b',
    re.I,
)
SHORT_DATE_RE = re.compile(
    r'\b((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|'
    r'Aug(?:ust)?|Sept(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
    r'\.?\s+\d{1,2},\s+\d{4})\b',
    re.I,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP])\.?M\.?\b', re.I)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r' *\n+ *', '\n', text).strip()


def parse_date(value):
    value = re.sub(r'\bSept\.?\b', 'Sep', value.strip(), flags=re.I)
    value = value.replace('.', '')
    for fmt in ('%B %d, %Y', '%b %d, %Y'):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_time(match):
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).upper() == 'P' else 0)
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def page_title(soup):
    heading = soup.find('h1')
    if heading and clean_text(heading):
        return clean_text(heading)
    title = clean_text(soup.title)
    return re.sub(r'\s*[-–|]\s*American Classical Orchestra.*$', '', title).strip()


def event_location(text, date_match, time_match):
    # Detail pages publish a compact "date / time / venue" line.  Limit the
    # search to its immediate neighbourhood so footer addresses are excluded.
    start = date_match.start() if date_match else 0
    snippet = text[start:start + 350]
    lines = [line.strip(' /|') for line in snippet.splitlines() if line.strip(' /|')]
    venue = None
    if time_match:
        tail = snippet[time_match.end() - start:]
        tail = re.sub(r'^[\s/|–—-]+', '', tail)
        venue = tail.split('\n', 1)[0].strip(' /|')
    if not venue or len(venue) > 140:
        for index, line in enumerate(lines):
            if TIME_RE.search(line):
                remainder = TIME_RE.sub('', line).strip(' /|–—-')
                if remainder:
                    venue = remainder
                elif index + 1 < len(lines):
                    venue = lines[index + 1]
                break
    if not venue:
        return None
    venue = re.split(r'\b(?:BUY TICKETS|TICKETS|About)\b', venue, maxsplit=1, flags=re.I)[0]
    venue = venue.strip(' ,/|–—-')
    if (
        not venue
        or TIME_RE.fullmatch(venue)
        or SHORT_DATE_RE.search(venue)
        or re.fullmatch(r'(?:a )?salon concert,?', venue, re.I)
    ):
        return None
    return venue


def record_from_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    for element in soup.select('nav, header, footer, script, style, noscript'):
        element.decompose()
    text = clean_text(soup)
    date_match = DATE_RE.search(text) or SHORT_DATE_RE.search(text)
    if not date_match:
        return None
    date = parse_date(date_match.group(1))
    time_match = TIME_RE.search(text, date_match.end(), date_match.end() + 180)
    venue = event_location(text, date_match, time_match)
    title = page_title(soup)
    if not title or not date or not venue:
        return None

    description = text
    if len(description) > 30000:
        description = description[:30000]
    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': parse_time(time_match),
        'venue': venue,
        'city': 'New York',
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def event_links(html, base_url):
    soup = BeautifulSoup(html, 'html.parser')
    links = set()
    allowed_paths = (
        '/season-', '/season-tickets/', '/reunion-', '/restore-', '/revisit-',
    )
    for anchor in soup.select('a[href]'):
        url = urljoin(base_url, anchor.get('href')).split('#', 1)[0]
        parsed = urlparse(url)
        if parsed.netloc == 'aconyc.org' and parsed.path.startswith(allowed_paths):
            links.add(url)
    return links


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    listing_html = []
    for url in (SOURCE_URL, ARCHIVE_URL):
        response = session.get(url, timeout=45)
        response.raise_for_status()
        listing_html.append((url, response.text))

    links = set()
    for base_url, html in listing_html:
        links.update(event_links(html, base_url))

    records = []
    for url in sorted(links):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            # Several obsolete archive links currently redirect to the season
            # landing page.  Treating that landing page as each old event would
            # manufacture duplicate records with the wrong date and programme.
            if urlparse(response.url).path.rstrip('/') != urlparse(url).path.rstrip('/'):
                log_message(
                    'Skipping redirected ACO archive link',
                    event='crawler_record_skipped', level='warning', url=url,
                )
                continue
            record = record_from_page(response.text, url)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping ACO page without a complete event location or date',
                    event='crawler_record_skipped', level='warning', url=url,
                )
        except requests.RequestException as error:
            log_message(
                'Failed to fetch ACO event page', event='crawler_page_failed',
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
            'No valid ACO concerts found', event='crawler_empty_listing',
            level='warning', url=SOURCE_URL, record_count=0,
        )
    return result


class AconycOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='aconyc_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'url'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    AconycOrgCrawler().run()


if __name__ == '__main__':
    main()
