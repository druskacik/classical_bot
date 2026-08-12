import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://nottchamberfest.com/'
SITEMAP_URL = f'{SOURCE_URL}events-sitemap.xml'
SOURCE = 'Nottingham Chamber Music Festival'
CITY = 'Nottingham'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-GB,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(\d{1,2})\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(20\d{2})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', re.IGNORECASE)


def clean_text(value, separator=' '):
    if not value:
        return ''
    text = value.get_text(separator, strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_date(text):
    match = DATE_RE.search(text)
    if not match:
        return None
    try:
        return datetime.strptime(' '.join(match.groups()), '%d %B %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour not in range(1, 13) or minute > 59:
        return None
    if match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def event_urls(content):
    soup = BeautifulSoup(content, 'xml')
    return list(
        dict.fromkeys(
            clean_text(node)
            for node in soup.find_all('loc')
            if '/events/' in clean_text(node)
        )
    )


def parse_event(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    article = soup.select_one('article.events')
    if not article:
        return None

    title = clean_text(article.select_one('h1'))
    date_text = clean_text(article.select_one('.event-dates'))
    event_date = parse_date(date_text)

    location_box = article.select_one('.et-box-content')
    location_lines = []
    if location_box:
        first_paragraph = location_box.select_one('p')
        location_lines = [
            clean_text(part)
            for part in first_paragraph.stripped_strings
            if clean_text(part)
        ] if first_paragraph else []
    venue = location_lines[0] if location_lines else None

    description_node = article.select_one('.entry-content')
    if description_node:
        for unwanted in description_node.select('script, style, iframe, .tryb-widget'):
            unwanted.decompose()
    description = clean_text(description_node, separator='\n') or None

    if not title or not event_date or not venue:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(date_text) or parse_time(description or ''),
        'venue': venue,
        'city': CITY,
        'country_code': 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()

    records = []
    for url in event_urls(response.content):
        try:
            detail = session.get(url, timeout=45)
            detail.raise_for_status()
            record = parse_event(detail.content, url)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping event with incomplete required fields',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                )
        except requests.RequestException as error:
            log_message(
                'Failed to scrape Nottingham Chamber Music Festival event',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class NottchamberfestComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nottchamberfest_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
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
    NottchamberfestComCrawler().run()


if __name__ == '__main__':
    main()
