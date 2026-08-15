import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://athenssymphony.org/'
SOURCE = 'Athens Symphony Orchestra'
DEFAULT_VENUE = 'The Classic Center Theatre'
CITY = 'Athens'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'(?P<month>[A-Za-z]+)\.?\s*,?\s*(?P<day>\d{1,2})(?:st|nd|rd|th)?\s*,?\s*'
    r'(?P<year>20\d{2})\s*(?:@|at)?\s*'
    r'(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<period>[ap])\.?m\.?',
    re.IGNORECASE,
)

MONTHS = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6, 'jul': 7,
    'july': 7, 'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9,
    'oct': 10, 'october': 10, 'nov': 11, 'november': 11,
    'dec': 12, 'december': 12,
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_occurrences(text):
    occurrences = []
    for match in DATE_TIME_RE.finditer(text):
        month = MONTHS.get(match.group('month').lower())
        if month is None:
            continue
        hour = int(match.group('hour'))
        if hour < 1 or hour > 12:
            continue
        if match.group('period').lower() == 'p' and hour != 12:
            hour += 12
        elif match.group('period').lower() == 'a' and hour == 12:
            hour = 0
        try:
            event_date = datetime(
                int(match.group('year')), month, int(match.group('day'))
            ).date().isoformat()
        except ValueError:
            continue
        occurrence = (event_date, f'{hour:02d}:{int(match.group("minute")):02d}')
        if occurrence not in occurrences:
            occurrences.append(occurrence)
    return occurrences


def discover_event_pages(soup):
    pages = {}
    for item in soup.select('li.menu-item-object-page'):
        link = item.select_one('a[href]')
        if link is None:
            continue
        url = urljoin(SOURCE_URL, link['href'].strip())
        parsed = urlparse(url)
        if parsed.netloc != urlparse(SOURCE_URL).netloc:
            continue
        path = parsed.path.rstrip('/') + '/'
        if path.startswith('/concerts/') and path != '/concerts/':
            pages[url] = clean_text(link)
        elif path == '/frankenstein-fundraiser/':
            pages[url] = clean_text(link)
    return pages


def parse_event_page(soup, url, fallback_title):
    content = soup.select_one('section#content')
    if content is None:
        return []
    text = clean_text(content)
    occurrences = parse_occurrences(text)
    if not occurrences:
        return []

    title_node = content.select_one('.featured-concert h2, article h1')
    title = clean_text(title_node) or fallback_title
    venue_match = re.search(
        r'(?im)^(?:venue:\s*)?(The\s+)?Classic Center (?:Theatre|Grand Hall)\s*$',
        text,
    )
    venue = venue_match.group(0).removeprefix('Venue:').strip() if venue_match else DEFAULT_VENUE
    description = text or None

    return [
        {
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
        }
        for event_date, time_from in occurrences
    ]


class AthenssymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='athenssymphony_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(SOURCE_URL, timeout=45)
            response.raise_for_status()
            pages = discover_event_pages(BeautifulSoup(response.text, 'html.parser'))
            if not pages:
                raise ValueError('Could not find any pages in the Concerts menu')

            records = []
            for url, fallback_title in pages.items():
                event_response = session.get(url, timeout=45)
                event_response.raise_for_status()
                records.extend(parse_event_page(
                    BeautifulSoup(event_response.text, 'html.parser'), url, fallback_title
                ))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Athens Symphony concerts',
                event='crawler_fetch_failed',
                level='error',
                url=getattr(getattr(error, 'request', None), 'url', SOURCE_URL),
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        return sorted(records, key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ))


def main():
    AthenssymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
