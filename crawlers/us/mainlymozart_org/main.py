import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mainlymozart.org/'
SOURCE = 'Mainly Mozart'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*)?'
    r'([A-Z][a-z]+\s+\d{1,2},\s+20\d{2})'
)
TIME_RE = re.compile(r'(?:Begins at|@)\s*(\d{1,2}(?::\d{2})?\s*[AP]M)', re.I)

# These are the venue labels used by the site's archived monthly calendars.
# The city is explicit in the label or is the venue's stable municipality.
VENUE_CITIES = {
    'Fairbanks Ranch Country Club': 'Rancho Santa Fe',
    'The Meridian, Downtown San Diego': 'San Diego',
    'Torrey Pines High School': 'San Diego',
    'Eve in Downtown San Diego': 'San Diego',
    'Dove Library in Carlsbad': 'Carlsbad',
}

CTA_RE = re.compile(
    r'^(?:purchase|tickets?|learn more|join club|free event|request discount|'
    r'donate|sign up|view (?:full|next|august)|we respect your privacy)', re.I
)


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_date(value):
    match = DATE_RE.search(value)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    raw = re.sub(r'\s*([AP]M)$', r' \1', match.group(1).upper())
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(raw, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def calendar_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    urls = []
    for node in soup.find_all('loc'):
        url = clean_text(node.get_text())
        if re.fullmatch(r'https://www\.mainlymozart\.org/purchase(?:-[a-z]+\d+)?', url):
            urls.append(url)
    return sorted(set(urls))


def section_record(section, page_url):
    text = clean_text(section.get_text(' ', strip=True))
    event_date = parse_date(text)
    if not event_date:
        return None

    date_match = DATE_RE.search(text)
    before_date = text[:date_match.start()].strip(' |')
    venue = next(
        (name for name in VENUE_CITIES if before_date.endswith(name)),
        None,
    )
    if not venue:
        return None

    headings = [clean_text(node.get_text(' ', strip=True)) for node in section.select('h1,h2,h3,h4')]
    title = next((heading for heading in headings if heading and heading != venue), '')
    if not title:
        return None

    parts = []
    for node in section.select('h1,h2,h3,h4,p'):
        part = clean_text(node.get_text(' ', strip=True))
        if part and not CTA_RE.match(part) and part not in parts:
            parts.append(part)

    return {
        'title': title,
        'date': event_date,
        'url': page_url,
        'time_from': parse_time(text),
        'venue': venue,
        'city': VENUE_CITIES[venue],
        'country_code': 'US',
        'description': '\n\n'.join(parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []

    for url in calendar_urls(session):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Calendar page request failed',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue

        soup = BeautifulSoup(response.text, 'html.parser')
        for section in soup.select('section.page-section'):
            record = section_record(section, url)
            if record:
                records.append(record)

    records.sort(key=lambda item: (item['date'], item['title'], item['venue']))
    return records


class MainlyMozartOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mainlymozart_org',
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
    MainlyMozartOrgCrawler().run()


if __name__ == '__main__':
    main()
