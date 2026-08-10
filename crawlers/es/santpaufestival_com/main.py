import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.santpaufestival.com/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
SOURCE = 'Festival Sant Pau'
VENUE_DEFAULT = 'Recinte Modernista de Sant Pau'
CITY_DEFAULT = 'Barcelona'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en,ca;q=0.9,es;q=0.8',
}
MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_urls(soup, prefix):
    urls = []
    seen = set()
    for link in soup.select('main a[href]'):
        url = urljoin(SOURCE_URL, link.get('href'))
        parsed = urlparse(url)
        path = parsed.path
        if (
            parsed.netloc == urlparse(SOURCE_URL).netloc
            and path.startswith(prefix) and path != prefix and url not in seen
        ):
            seen.add(url)
            urls.append(url)
    return urls


def parse_dates(text):
    dates = []
    # Current detail pages use, for example, "SEPTEMBER 22&23 2026".
    pattern = re.compile(
        r'\b(' + '|'.join(MONTHS) + r')\s+(\d{1,2})(?:\s*(?:&|and|-)\s*(\d{1,2}))?'
        r'(?:st|nd|rd|th)?\s*,?\s*(20\d{2})\b',
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        month = MONTHS[match.group(1).lower()]
        for day in (match.group(2), match.group(3)):
            if not day:
                continue
            try:
                value = datetime(int(match.group(4)), month, int(day)).date().isoformat()
            except ValueError:
                continue
            if value not in dates:
                dates.append(value)

    # Archived Squarespace calendar items use "Tuesday 16 September 2025".
    archive = re.compile(
        r'\b(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\s+(20\d{2})\b',
        re.IGNORECASE,
    )
    for match in archive.finditer(text):
        try:
            value = datetime(
                int(match.group(3)), MONTHS[match.group(2).lower()], int(match.group(1))
            ).date().isoformat()
        except ValueError:
            continue
        if value not in dates:
            dates.append(value)
    return dates


def extract_time(text):
    matches = re.findall(r'\b([01]?\d|2[0-3]):([0-5]\d)\s*[hH]?\b', text)
    return f'{int(matches[0][0]):02d}:{matches[0][1]}' if matches else None


def parse_current_detail(soup, url):
    main = soup.select_one('main')
    if not main:
        return []
    text = clean_text(main.get_text('\n', strip=True))
    dates = parse_dates(text)
    title_node = main.select_one('h1, h2, h3')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    time_from = extract_time(text)

    venue = VENUE_DEFAULT
    city = CITY_DEFAULT
    date_line = next((line for line in text.splitlines() if re.search(r'20\d{2}', line)), '')
    lines = text.splitlines()
    if date_line in lines:
        index = lines.index(date_line)
        if index + 1 < len(lines):
            venue_line = re.sub(r'\s*-\s*\d{1,2}:\d{2}\s*[hH]?\s*$', '', lines[index + 1]).strip()
            if venue_line:
                venue = venue_line
    if 'palau de la musica' in venue.casefold() or 'palau de la música' in venue.casefold():
        city = 'Barcelona'
    elif 'sant pau' not in venue.casefold():
        # All published festival locations are currently in Barcelona, but do
        # not silently apply the home venue to a differently named location.
        city = CITY_DEFAULT

    if not title or not dates or not venue or not city:
        return []
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'ES',
            'description': text or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
    ]


def parse_calendar_detail(soup, url):
    main = soup.select_one('main')
    if not main:
        return []
    text = clean_text(main.get_text('\n', strip=True))
    dates = parse_dates(text)
    if not dates:
        return []
    lines = text.splitlines()
    title = next((line for line in lines if line and line != 'Back to All Events'), '')
    time_from = extract_time(text)
    venue = next((line for line in lines if 'Sant Pau' in line and 'Festival' not in line), VENUE_DEFAULT)
    city = next((line.split(',')[0] for line in lines if re.match(r'^Barcelona\b', line, re.I)), CITY_DEFAULT)
    description_start = 0
    for index, line in enumerate(lines):
        if line in {'ICS', 'Google Calendar'}:
            description_start = index + 1
    description = clean_text('\n'.join(lines[description_start:])) or None
    if not title or not venue or not city:
        return []
    return [{
        'title': title,
        'date': dates[0],
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'ES',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    sources = (
        (EVENTS_URL, '/', parse_current_detail),
        (CALENDAR_URL, '/calendar/', parse_calendar_detail),
    )
    for listing_url, prefix, parser in sources:
        try:
            listing = get_soup(session, listing_url)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape concert listing', event='crawler_listing_failed', level='warning',
                url=listing_url, error_type=type(error).__name__, error_message=str(error),
            )
            continue
        urls = listing_urls(listing, prefix)
        if listing_url == EVENTS_URL:
            urls = [url for url in urls if urlparse(url).path not in {
                '/about', '/artists', '/contact', '/home', '/events', '/abonaments',
                '/privacy-policy', '/privacy-policy-1',
            }]
        for url in urls:
            try:
                records.extend(parser(get_soup(session, url), url))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail', event='crawler_item_failed', level='warning',
                    url=url, error_type=type(error).__name__, error_message=str(error),
                )

    unique = {}
    for record in records:
        key = (record['date'], record['time_from'], record['venue'], record['title'])
        unique[key] = record
    return sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class SantPauFestivalComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='santpaufestival_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
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
    SantPauFestivalComCrawler().run()


if __name__ == '__main__':
    main()
