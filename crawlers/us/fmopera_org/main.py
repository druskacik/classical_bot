import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.fmopera.org/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Fargo-Moorhead Opera'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:\s*,)?\s+(?P<year>20\d{2})'
    r'(?:\s*(?:@|at)?\s*(?P<time>\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?))?',
    re.IGNORECASE,
)

# FM Opera performs around the Fargo-Moorhead metro rather than in one fixed hall.
# Only known first-party venue names are mapped; pages with an unknown venue are skipped.
VENUE_CITIES = {
    'NDSU Challey School of Music, Festival Hall': 'Fargo',
    'Festival Concert Hall': 'Fargo',
    'Festival Hall': 'Fargo',
    'Fargo Theatre': 'Fargo',
    'Fargo-Moorhead Community Theatre': 'Fargo',
    'The Stage at Island Park': 'Fargo',
    'Reineke Fine Arts Center': 'Fargo',
    'NDSU': 'Fargo',
    'Hjemkomst Center': 'Moorhead',
    'Weld Hall, MSUM': 'Moorhead',
    'Weld Hall': 'Moorhead',
    'MSUM': 'Moorhead',
    'Bluestem Center for the Arts': 'Moorhead',
    'Bluestem Amphitheater': 'Moorhead',
    'Kindred Performing Arts Center': 'Kindred',
}

IGNORE_PATHS = {
    '', 'about', 'news', 'contact', 'partners', 'take-action', 'board-of-directors',
    'what-we-do', 'past-artists', 'donate-now', 'tickets', 'yap-auditions-wording',
    'hero-image', 'banner-royce', 'about-intro-mojave', 'current-artists-copy',
    'subscription-mail-order-form', 'become-a-sponsor', 'seasoninformation',
    'home', 'home-copy', 'home-copy-3', 'home-copy-4', 'new-cover-page-1-1',
    'cover-page-18-19-1',
}


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    if not value:
        return None
    normalized = re.sub(r'\.', '', value).strip().upper()
    for pattern in ('%I:%M%p', '%I%p', '%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def venue_from_lines(lines, date_index):
    def venue_on_line(line):
        for venue, city in sorted(VENUE_CITIES.items(), key=lambda item: -len(item[0])):
            if venue.lower() in line.lower():
                return venue, city
        return None

    # Current pages place a venue before each group of performance dates.
    for index in range(date_index - 1, max(-1, date_index - 9), -1):
        venue = venue_on_line(lines[index])
        if venue:
            return venue
    # Retained older pages commonly put one shared venue after both dates.
    for index in range(date_index + 1, min(len(lines), date_index + 9)):
        venue = venue_on_line(lines[index])
        if venue:
            return venue
    return None, None


def page_records(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main') or soup.body
    if not main:
        return []
    text = clean_text(main.get_text('\n', strip=True))
    lines = [line for line in text.splitlines() if line]
    matches = [(index, match) for index, line in enumerate(lines) for match in DATE_RE.finditer(line)]
    if not matches:
        return []

    title = clean_text((soup.title.string if soup.title else '').split('—')[0])
    if not title:
        return []

    description = text or None
    records = []
    for line_index, match in matches:
        venue, city = venue_from_lines(lines, line_index)
        if not venue or not city:
            continue
        try:
            event_date = datetime.strptime(
                f"{match.group('month')} {match.group('day')} {match.group('year')}",
                '%B %d %Y',
            ).date().isoformat()
        except ValueError:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(match.group('time')),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def sitemap_pages(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    pages = []
    for node in soup.find_all('loc'):
        url = clean_text(node.get_text())
        parsed = urlparse(url)
        path = parsed.path.strip('/')
        if parsed.netloc != 'www.fmopera.org' or path in IGNORE_PATHS or path.startswith('news/'):
            continue
        pages.append(url)
    return list(dict.fromkeys(pages))


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url in sitemap_pages(session):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            records.extend(page_records(url, response.text))
        except requests.RequestException as error:
            log_message(
                'Production page request failed',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        unique[key] = record
    result = sorted(unique.values(), key=lambda item: (item['date'], item['title'], item['time_from'] or ''))
    if not result:
        log_message(
            'No production occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
    return result


class FmoperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fmopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    FmoperaOrgCrawler().run()


if __name__ == '__main__':
    main()
