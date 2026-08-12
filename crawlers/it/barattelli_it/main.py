import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://barattelli.it/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendario/')
SOURCE = 'Ente Musicale Società Aquilana dei Concerti B. Barattelli'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}


def clean_text(value, separator='\n'):
    if value is None:
        return ''
    text = value.get_text(separator, strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def season_urls(soup):
    urls = []
    for link in soup.select('a[href*="stagione="]'):
        url = urljoin(CALENDAR_URL, link.get('href', ''))
        values = parse_qs(urlparse(url).query).get('stagione', [])
        if values and values[0].isdigit() and url not in urls:
            urls.append(url)
    return urls or [CALENDAR_URL]


def event_urls(soup):
    urls = []
    for link in soup.select('a[href*="/eventi/"]'):
        url = urljoin(SOURCE_URL, link.get('href', '')).split('#', 1)[0]
        if url.startswith(urljoin(SOURCE_URL, 'eventi/')) and url not in urls:
            urls.append(url)
    return urls


def parse_date_time(value):
    match = re.search(
        r'\b(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\s+(\d{4})'
        r'(?:\s*,?\s*ore\s+(\d{1,2})[.:](\d{2}))?',
        value,
        re.I,
    )
    if not match:
        return None
    try:
        event_date = date(
            int(match.group(3)), MONTHS[match.group(2).casefold()], int(match.group(1))
        ).isoformat()
        time_from = None
        if match.group(4) is not None:
            hour, minute = int(match.group(4)), int(match.group(5))
            if hour > 23 or minute > 59:
                return None
            time_from = f'{hour:02d}:{minute:02d}'
        return event_date, time_from
    except ValueError:
        return None


def parse_location(value):
    parts = [part.strip() for part in re.split(r'\s+[|]\s+', value) if part.strip()]
    if len(parts) < 2:
        return None
    location = parts[-2]
    location_parts = [part.strip() for part in re.split(r'\s+-\s+', location) if part.strip()]
    if len(location_parts) < 2:
        return None

    city, venue = location_parts[0], location_parts[1]
    city = re.sub(r'\s*\((?:AQ|L[’\']Aquila)\)\s*$', '', city, flags=re.I).strip()
    if len(location_parts) > 2 and not re.match(
        r'^(?:corso|via|viale|largo)\b', location_parts[2], re.I
    ):
        venue = ' - '.join(location_parts[1:])
    if not city or not venue:
        return None
    return city, venue


def parse_detail(soup, url):
    title = clean_text(soup.select_one('h1'), ' ')
    meta = next(
        (
            clean_text(node, ' ')
            for node in soup.select('section:first-of-type p')
            if parse_date_time(clean_text(node, ' '))
        ),
        '',
    )
    date_time = parse_date_time(meta)
    location = parse_location(meta)
    if not title or not date_time or not location:
        return None

    description_node = soup.select_one('section.bg-charcoal-blue')
    description = clean_text(description_node) or None
    event_date, time_from = date_time
    city, venue = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class BarattelliItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='barattelli_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            calendar_soup = get_soup(CALENDAR_URL)
            calendars = season_urls(calendar_soup)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Barattelli calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        details = []
        for calendar in calendars:
            try:
                soup = calendar_soup if calendar.rstrip('/') == CALENDAR_URL.rstrip('/') else get_soup(calendar)
                for url in event_urls(soup):
                    if url not in details:
                        details.append(url)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Barattelli season',
                    event='crawler_item_failed',
                    level='warning',
                    url=calendar,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(get_soup, url): url for url in details}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = parse_detail(future.result(), url)
                    if record:
                        records.append(record)
                except (requests.RequestException, TypeError, ValueError) as error:
                    log_message(
                        'Failed to parse Barattelli event',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    BarattelliItCrawler().run()


if __name__ == '__main__':
    main()
