import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.orchestra.sg/'
SOURCE = 'Orchestra of the Music Makers Singapore'
ARCHIVE_URL = urljoin(SOURCE_URL, 'past-performances')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-SG,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:(?:mon|tue|wed|thu|fri|sat|sun)(?:day)?,\s*)?'
    r'(\d{1,2})\s+'
    r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
    r'\s+(\d{4}),?\s+(\d{1,2})(?:[.:](\d{2}))?\s*([ap]m)',
    re.IGNORECASE,
)

SINGAPORE_VENUE_MARKERS = (
    'esplanade',
    'victoria concert hall',
    'sota',
    'school of the arts',
    'capitol theatre',
    'star theatre',
    'yong siew toh',
    'paragon',
    'botanic gardens',
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(session, url):
    response = session.get(url, params={'format': 'json'}, timeout=45)
    response.raise_for_status()
    payload = response.json()
    return BeautifulSoup(payload.get('mainContent') or '', 'html.parser')


def listing_links(session):
    links = set()
    # Squarespace's JSON page body omits the navigation folder, which is the
    # authoritative list of current concerts. The rendered document includes
    # that folder server-side, so no browser execution is needed.
    response = session.get(SOURCE_URL, timeout=45)
    response.raise_for_status()
    homepage = BeautifulSoup(response.text, 'html.parser')
    for folder in homepage.select('.folder'):
        if 'upcoming' not in clean_text(folder.get_text(' ', strip=True)).lower():
            continue
        for anchor in folder.select('.collection a[href]'):
            links.add(urljoin(SOURCE_URL, anchor.get('href')))

    # Follow any surviving archive detail links too. Month-only summaries are
    # intentionally rejected later because they lack a complete date/venue.
    archive = get_page(session, ARCHIVE_URL)
    for anchor in archive.select('a[href]'):
        links.add(urljoin(ARCHIVE_URL, anchor.get('href')))

    normalized = set()
    for url in links:
        parsed = urlparse(url)
        if parsed.netloc == 'www.orchestra.sg' and parsed.path not in ('/', '/past-performances'):
            normalized.add(f'{parsed.scheme}://{parsed.netloc}{parsed.path}')
    return sorted(normalized)


def parse_date(match):
    value = f'{match.group(1)} {match.group(2)} {match.group(3)}'
    event_date = datetime.strptime(value, '%d %b %Y').date().isoformat()
    hour = int(match.group(4)) % 12
    if match.group(6).lower() == 'pm':
        hour += 12
    return event_date, f'{hour:02d}:{int(match.group(5) or 0):02d}'


def find_venue(soup, matches):
    final_match = matches[-1]
    container = final_match.string
    remainder = clean_text(container[final_match.end():]).strip(' ,–—-')
    venue = remainder.split('\n', 1)[0].strip()
    if not venue:
        return None
    if not any(marker in venue.lower() for marker in SINGAPORE_VENUE_MARKERS):
        return None
    return venue


def parse_event(url, soup):
    title_tag = soup.find(['h1', 'h2'])
    title = clean_text(title_tag.get_text(' ', strip=True) if title_tag else '')
    if not title:
        return []

    matches = []
    for element in soup.find_all(['h1', 'h2', 'h3', 'p']):
        value = clean_text(element.get_text(' ', strip=True))
        for match in DATE_RE.finditer(value):
            matches.append(match)
    if not matches:
        return []

    venue = find_venue(soup, matches)
    if not venue:
        return []

    description = clean_text(soup.get_text('\n', strip=True)) or None
    records = []
    for match in matches:
        try:
            event_date, time_from = parse_date(match)
        except ValueError:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': 'Singapore',
            'country_code': 'SG',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url in listing_links(session):
        try:
            records.extend(parse_event(url, get_page(session, url)))
        except (requests.RequestException, ValueError, TypeError) as error:
            log_message(
                'Failed to scrape concert detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'], record['title'], record['url']),
    )


class OrchestraSgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestra_sg',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='SG',
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
        return get_concerts()


def main():
    OrchestraSgCrawler().run()


if __name__ == '__main__':
    main()
