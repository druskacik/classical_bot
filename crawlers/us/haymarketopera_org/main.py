import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.haymarketopera.org/'
SOURCE = 'Haymarket Opera Company'
ARCHIVE_URL = urljoin(SOURCE_URL, 'previous-events')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|November|December|'
    'Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec'
)
FULL_DATE_RE = re.compile(
    rf'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    rf'(?P<month>{MONTHS})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?'
    rf'(?:,|\s)\s*(?P<year>20\d{{2}})'
    rf'(?:\s*(?:-|–|—|at|,at)\s*(?P<time>\d{{1,2}}(?::\d{{2}})?\s*[ap]\.?m\.?))?',
    re.IGNORECASE,
)
RANGE_DATE_RE = re.compile(
    rf'(?P<month>{MONTHS})\.?\s+(?P<first>\d{{1,2}})(?:st|nd|rd|th)?\s*'
    rf'(?P<joiner>-|–|—|&|and|to)\s*(?P<last>\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*'
    rf'(?P<year>20\d{{2}})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?)\s*([ap])\.?m\.?(?!\w)', re.IGNORECASE)
CITY_RE = re.compile(r'\b([A-Z][A-Za-z .\'-]+),\s*IL\s+\d{5}\b')
NON_EVENT_RE = re.compile(
    r'\b(recording|album|broadcast|live on wfmt|stream|donat(?:e|ion)|match challenge)\b',
    re.IGNORECASE,
)


def clean_text(value):
    text = BeautifulSoup(str(value or ''), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def fetch_page_json(session, url):
    response = session.get(url, params={'format': 'json'}, timeout=45)
    response.raise_for_status()
    return response.json()


def event_urls(session):
    urls = set()
    response = session.get(SOURCE_URL, timeout=45)
    response.raise_for_status()
    homepage = BeautifulSoup(response.text, 'html.parser')
    for link in homepage.select('.subnav a[href]'):
        url = urljoin(SOURCE_URL, link.get('href'))
        parsed = urlparse(url)
        if parsed.netloc == 'www.haymarketopera.org':
            urls.add(f'{parsed.scheme}://{parsed.netloc}{parsed.path}')

    for listing_url in (ARCHIVE_URL,):
        data = fetch_page_json(session, listing_url)
        soup = BeautifulSoup(data.get('mainContent', ''), 'html.parser')
        for link in soup.select('a[href]'):
            url = urljoin(listing_url, link.get('href'))
            parsed = urlparse(url)
            if parsed.netloc == 'www.haymarketopera.org' and parsed.path not in {'/', '/previous-events'}:
                urls.add(f'{parsed.scheme}://{parsed.netloc}{parsed.path}')
    return sorted(urls)


def parse_time(value):
    match = TIME_RE.search(value or '')
    if not match:
        return None
    raw = f'{match.group(1)} {match.group(2).upper()}M'
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(raw, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def iso_date(month, day, year):
    month = month.rstrip('.')
    if month.lower() == 'sept':
        month = 'Sep'
    for pattern in ('%B %d %Y', '%b %d %Y'):
        try:
            return datetime.strptime(f'{month} {day} {year}', pattern).date().isoformat()
        except ValueError:
            pass
    return None


def dates_from_text(text):
    dates = []
    for match in FULL_DATE_RE.finditer(text):
        date = iso_date(match.group('month'), match.group('day'), match.group('year'))
        if date:
            dates.append((date, parse_time(match.group(0))))
    for match in RANGE_DATE_RE.finditer(text):
        first, last = int(match.group('first')), int(match.group('last'))
        if 0 < last - first <= 7:
            days = range(first, last + 1) if match.group('joiner') in {'-', '–', '—', 'to'} else (first, last)
            for day in days:
                date = iso_date(match.group('month'), day, match.group('year'))
                if date:
                    dates.append((date, None))
    return list(dict.fromkeys(dates))


def page_title(data, soup):
    heading = soup.find(['h1', 'h2'])
    title = clean_text(heading)
    if title:
        return title
    collection_title = clean_text((data.get('collection') or {}).get('title'))
    return re.sub(r'\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec).*$', '', collection_title)


def venue_and_city(elements):
    city = ''
    venue = ''
    for text in elements[:18]:
        city_match = re.search(r'\b(Chicago|Evanston|Highland Park|Oak Park|River Forest|Winnetka),\s*IL\b', text, re.I)
        if city_match:
            city = city_match.group(1).strip().title()
            before = text[:city_match.start()].strip(' ,|')
            before = re.sub(r'\s+\d{1,5}\s+[A-Z].*$', '', before).strip(' ,|')
            before = re.sub(r'^.*?(?:20\d{2}|[ap]\.?m\.?)\s*\|?\s*', '', before, flags=re.I).strip(' ,|')
            if before:
                venue = before.split('  ')[-1].strip()
            break
    if not city:
        for text in elements[:10]:
            if re.search(r'\bChicago\b', text, re.I):
                city = 'Chicago'
                break
    if not venue:
        for text in elements[:12]:
            match = re.search(r'20\d{2}\s*\|\s*([^|,]+(?:,\s*[^|,]+)?)', text)
            if match:
                venue = match.group(1).strip()
                break
    return venue, city


def parse_event(data, url):
    soup = BeautifulSoup(data.get('mainContent', ''), 'html.parser')
    title = page_title(data, soup)
    elements = [clean_text(node) for node in soup.select('h1,h2,h3,p')]
    elements = [text for text in elements if text]
    lead = ' '.join(elements[:12])
    if not title or NON_EVENT_RE.search(f'{title} {lead[:300]}'):
        return []

    # Event schedules and location details are consistently near the top of
    # Squarespace detail pages. Limiting the scan avoids dates in biographies.
    dates = dates_from_text(' '.join(elements[:10]))
    venue, city = venue_and_city(elements)
    if not dates or not venue or not city:
        return []

    description = '\n\n'.join(elements)
    occurrences = []
    for date, time_from in dates:
        parsed_date = datetime.strptime(date, '%Y-%m-%d')
        occurrence_venue, occurrence_city = venue, city
        for text in elements[:14]:
            date_match = re.search(
                rf'\b{parsed_date.strftime("%B")}\s+{parsed_date.day}\b', text, re.I
            )
            if not date_match:
                continue
            time_from = parse_time(text[date_match.start():date_match.start() + 70]) or time_from
            local_venue, local_city = venue_and_city([text])
            occurrence_venue = local_venue or occurrence_venue
            occurrence_city = local_city or occurrence_city
            if time_from:
                break
        occurrences.append((date, time_from, occurrence_venue, occurrence_city))

    timed_dates = {date for date, time_from, _, _ in occurrences if time_from}
    occurrences = [
        occurrence for occurrence in occurrences
        if occurrence[1] or occurrence[0] not in timed_dates
    ]

    return [{
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': occurrence_venue,
        'city': occurrence_city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for date, time_from, occurrence_venue, occurrence_city in occurrences]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    urls = event_urls(session)
    for url in urls:
        try:
            records.extend(parse_event(fetch_page_json(session, url), url))
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Could not parse Haymarket event page',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    if not records:
        log_message(
            'No Haymarket concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    unique = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        unique[key] = record
    return sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class HaymarketOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='haymarketopera_org',
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
    HaymarketOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
