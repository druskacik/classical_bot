import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://canterburymusicclub.com/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Canterbury Music Club'
VENUE = 'Colyer-Fergusson Hall'
CITY = 'Canterbury'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+'
    r'(\d{1,2})\s*(?:st|nd|rd|th)?\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(20\d{2})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})[.:](\d{2})\s*(am|pm)\b', re.IGNORECASE)
SEASON_PATH_RE = re.compile(r'^/concerts-\d{2}-\d{2}/?$')
EVENT_PATH_RE = re.compile(r'^/\d+\.[^/]+/?$')


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def sitemap_urls(session):
    soup = BeautifulSoup(get_response(session, SITEMAP_URL).content, 'xml')
    return [clean_text(node) for node in soup.select('url > loc')]


def event_urls(session):
    urls = sitemap_urls(session)
    season_urls = [url for url in urls if SEASON_PATH_RE.match(urlparse(url).path)]
    discovered = []
    for season_url in season_urls:
        soup = BeautifulSoup(get_response(session, season_url).content, 'html.parser')
        for anchor in soup.select('a[href]'):
            url = requests.compat.urljoin(season_url, anchor.get('href'))
            if EVENT_PATH_RE.match(urlparse(url).path):
                discovered.append(url)
    return list(dict.fromkeys(discovered))


def parse_date(match):
    day, month, year = match.groups()
    try:
        return datetime.strptime(f'{day} {month} {year}', '%d %B %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(text, start):
    match = TIME_RE.search(text, start)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour < 1 or hour > 12 or minute > 59:
        return None
    if match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def page_title(soup):
    title = clean_text(soup.title)
    title = title.split('|', 1)[0].strip()
    return re.sub(r'^\d+\.\s*', '', title).strip(' \"“”\'')


def parse_event(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    title = page_title(soup)
    text = clean_text(soup.body)
    date_match = DATE_RE.search(text)
    if not title or not date_match:
        return None
    event_date = parse_date(date_match)
    if not event_date:
        return None

    description = text[date_match.end():]
    description = re.split(r'\nTickets from\n', description, maxsplit=1, flags=re.IGNORECASE)[0]
    description = description.strip() or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(text, date_match.end()),
        'venue': VENUE,
        'city': CITY,
        'country_code': 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url in event_urls(session):
        try:
            record = parse_event(get_response(session, url).content, url)
            if record:
                records.append(record)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape Canterbury Music Club event detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(records, key=lambda record: (record['date'], record['title'], record['url']))


class CanterburyMusicClubComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='canterburymusicclub_com',
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
    CanterburyMusicClubComCrawler().run()


if __name__ == '__main__':
    main()
