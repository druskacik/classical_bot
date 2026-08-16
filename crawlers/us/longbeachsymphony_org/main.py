import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://longbeachsymphony.org/'
SOURCE = 'Long Beach Symphony'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# These are first-party series labels on the calendar.  The separate
# ``special-events`` feed includes nonclassical events such as Jazz by the Bay.
IN_SCOPE_SERIES = {'classical', 'pops', 'soundwaves'}
VENUE_PATTERN = re.compile(
    r'\b(?:theat(?:er|re)|arena|amphitheat(?:er|re)|concert hall|auditorium)\b',
    re.IGNORECASE,
)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_and_time(value):
    match = re.search(
        r'\b([A-Z][a-z]{2})\s+(\d{1,2}),\s+(20\d{2})'
        r'(?:\s+(\d{1,2}):([0-5]\d)\s*([AP]M))?',
        value,
    )
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(
            ' '.join(match.group(1, 2, 3)), '%b %d %Y'
        ).date().isoformat()
    except ValueError:
        return None, None

    time_from = None
    if match.group(4):
        time_from = datetime.strptime(
            f'{match.group(4)}:{match.group(5)} {match.group(6)}', '%I:%M %p'
        ).strftime('%H:%M')
    return event_date, time_from


def parse_location(soup):
    details = soup.select_one('section.concerts-events .side-column p.details')
    lines = [line.strip(' ,-') for line in clean_text(details).splitlines() if line.strip()]
    venue = next((line for line in lines if VENUE_PATTERN.search(line)), None)
    city_line = next((line for line in lines if re.search(r'\bLong Beach,\s*CA\b', line)), None)
    if not venue or not city_line:
        return None
    venue = re.split(r'\s+[–—-]\s+(?=\d)', venue, maxsplit=1)[0].strip()
    return venue, 'Long Beach'


def parse_event(response):
    soup = BeautifulSoup(response.text, 'html.parser')
    section = soup.select_one('section.concerts-events')
    content = section.select_one('.main-column .content') if section else None
    title_node = section.select_one('h1') if section else None
    date_node = content.select_one('em') if content else None
    title = clean_text(title_node)
    event_date, time_from = parse_date_and_time(clean_text(date_node))
    if not time_from:
        start_match = re.search(
            r'concert starts at\s+(\d{1,2})(?::([0-5]\d))?\s*([AP]M)',
            clean_text(content),
            re.IGNORECASE,
        )
        if start_match:
            time_from = datetime.strptime(
                f'{start_match.group(1)}:{start_match.group(2) or "00"} '
                f'{start_match.group(3).upper()}',
                '%I:%M %p',
            ).strftime('%H:%M')
    location = parse_location(soup)
    if not title or not event_date or not location:
        return None

    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': response.url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(content) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class LongBeachSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='longbeachsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(CALENDAR_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Long Beach Symphony calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        event_urls = []
        for article in soup.select('section.calendar article.event'):
            if not IN_SCOPE_SERIES.intersection(article.get('class', [])):
                continue
            link = article.select_one('a[href*="/concerts-events/"]')
            if link:
                event_urls.append(urljoin(CALENDAR_URL, link.get('href', '')))

        records = []
        for url in dict.fromkeys(event_urls):
            try:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Long Beach Symphony event',
                    event='crawler_fetch_failed',
                    level='error',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            record = parse_event(detail_response)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    LongBeachSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
