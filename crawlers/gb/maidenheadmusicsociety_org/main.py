import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.maidenheadmusicsociety.org/'
HISTORY_URL = urljoin(SOURCE_URL, 'history')
SOURCE = 'Maidenhead Music Society'
VENUE = 'Norden Farm Centre for the Arts'
CITY = 'Maidenhead'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|'
    'November|December'
)
DATE_RE = re.compile(
    rf'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*'
    rf'(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTHS})\s+(20\d{{2}})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?:[.:](\d{2}))\s*(am|pm)\b', re.IGNORECASE)


def clean_text(value, separator=' '):
    if not value:
        return ''
    text = value.get_text(separator, strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def parse_date(match):
    try:
        return datetime.strptime(' '.join(match.groups()), '%d %B %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour)
    minute = int(minute)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if meridiem.lower() == 'pm' and hour != 12:
        hour += 12
    elif meridiem.lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def season_urls(session):
    soup = get_soup(session, SOURCE_URL)
    urls = []
    for link in soup.select('a[href]'):
        url = urljoin(SOURCE_URL, link.get('href'))
        if re.fullmatch(r'https://www\.maidenheadmusicsociety\.org/concert-season-20\d{2}-(?:20)?\d{2}/?', url):
            urls.append(url.rstrip('/'))
    return list(dict.fromkeys(urls))


def detail_urls(session, season_url):
    soup = get_soup(session, season_url)
    main = soup.select_one('main')
    if not main:
        return []
    urls = []
    for link in main.select('a[href]'):
        url = urljoin(season_url, link.get('href')).rstrip('/')
        if (
            url.startswith(SOURCE_URL)
            and url != season_url.rstrip('/')
            and not url.endswith(('.pdf', '.jpg', '.png'))
        ):
            urls.append(url)
    return list(dict.fromkeys(urls))


def parse_detail(soup, url):
    main = soup.select_one('main')
    if not main:
        return None
    text = clean_text(main)
    date_match = DATE_RE.search(text)
    event_date = parse_date(date_match) if date_match else None
    if not event_date or re.search(r'\b(?:for MMS Members|MMS Members only|private event)\b', text, re.I):
        return None

    title = clean_text(soup.title).split('|', 1)[0].strip()
    if not title:
        title = clean_text(main.select_one('h1, h2'))
    if not title:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(text),
        'venue': VENUE,
        'city': CITY,
        'country_code': 'GB',
        'description': text or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def history_records(session):
    soup = get_soup(session, HISTORY_URL)
    main = soup.select_one('main')
    text = clean_text(main)
    matches = list(DATE_RE.finditer(text))
    records = []
    for index, match in enumerate(matches):
        event_date = parse_date(match)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        title = text[match.end():end].strip(' .:-')
        title = re.sub(r'\s+\d{4}-\d{4}\s+-\s+\d+(?:st|nd|rd|th)\s+Season.*$', '', title)
        if not event_date or not title:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': HISTORY_URL,
            'time_from': None,
            'venue': VENUE,
            'city': CITY,
            'country_code': 'GB',
            'description': title,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = history_records(session)
    for season_url in season_urls(session):
        for url in detail_urls(session, season_url):
            try:
                record = parse_detail(get_soup(session, url), url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Maidenhead Music Society concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class MaidenheadMusicSocietyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='maidenheadmusicsociety_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    MaidenheadMusicSocietyOrgCrawler().run()


if __name__ == '__main__':
    main()
