import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lcsymphony.com/'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
SOURCE = 'Lake Charles Symphony'
CITY = 'Lake Charles'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|'
    'October|November|December'
)
DATE_RE = re.compile(rf'\b({MONTHS})\s+(\d{{1,2}}),?\s+(20\d{{2}})\b', re.I)
CONCERT_EVIDENCE_RE = re.compile(
    r'\b(concert|orchestra|symphony|symphonic|recital|program(?:me)?)\b', re.I
)
NON_EVENT_RE = re.compile(
    r'\b(member(?:ship)?|sponsor|advertising|fundraiser|brunch|bingo|photo gallery)\b',
    re.I,
)
VENUE_PATTERNS = (
    re.compile(r'\bat (?:the )?([A-Z][^.!?\n]{2,80}?(?:Center|Theatre|Theater|Hall|Cathedral|Park))\b'),
    re.compile(r'\b(?:venue|location)\s*:\s*([^\n|]{3,100})', re.I),
)


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True) if '<' in raw else raw
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def sitemap_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'xml')
    urls = []
    for location in soup.find_all('loc'):
        url = clean_text(location.get_text())
        if url.startswith(SOURCE_URL) and url not in urls:
            urls.append(url)
    return urls


def page_description(soup):
    main = soup.select_one('main, #content, .jtpl-content') or soup.body
    return clean_text(main.get_text('\n', strip=True) if main else '')


def event_title(text, soup):
    # LCSO commonly introduces its event with "Event Description" followed by
    # a sentence containing a fully styled event name. Preserve that name rather
    # than the often-generic Jimdo browser title.
    match = re.search(
        r'\b(Summer Pops\s+20\d{2}\s*:\s*America\s+250\s*(?:\.{0,3}|…)\s*The Soundtrack)',
        text,
        re.I,
    )
    if match:
        return re.sub(r'\s+', ' ', clean_text(match.group(1))).rstrip(' .')

    heading = soup.select_one('h1')
    title = clean_text(heading.get_text(' ', strip=True) if heading else '')
    if not title:
        title = clean_text(soup.title.get_text(' ', strip=True) if soup.title else '')
        title = re.sub(r'\s+-\s+Lake Charles Symphony$', '', title, flags=re.I)
    if not title or NON_EVENT_RE.search(title):
        return ''
    return title


def event_venue(text):
    for pattern in VENUE_PATTERNS:
        match = pattern.search(text)
        if match:
            venue = clean_text(match.group(1)).strip(' ,;-')
            if venue and venue.lower() != CITY.lower():
                return venue
    return ''


def parse_page(url, soup):
    text = page_description(soup)
    if not text or not CONCERT_EVIDENCE_RE.search(text):
        return []

    title = event_title(text, soup)
    venue = event_venue(text)
    if not title or not venue:
        return []

    time_match = re.search(
        r'\b(?:concert begins(?: promptly)? at|begins at|concert at)\s*'
        r'(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?',
        text,
        re.I,
    )
    time_from = None
    if time_match:
        hour = int(time_match.group(1)) % 12
        if time_match.group(3).lower() == 'p':
            hour += 12
        time_from = f'{hour:02d}:{int(time_match.group(2) or 0):02d}'

    records = []
    seen_dates = set()
    for match in DATE_RE.finditer(text):
        try:
            event_date = datetime.strptime(
                f'{match.group(1)} {match.group(2)} {match.group(3)}', '%B %d %Y'
            ).date().isoformat()
            date.fromisoformat(event_date)
        except ValueError:
            continue
        if event_date in seen_dates:
            continue
        seen_dates.add(event_date)
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': text,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url in sitemap_urls(session):
        try:
            records.extend(parse_page(url, get_soup(session, url)))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape concert page',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class LcsymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lcsymphony_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    LcsymphonyComCrawler().run()


if __name__ == '__main__':
    main()
