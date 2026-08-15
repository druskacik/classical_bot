import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://bemf.org/'
SEASON_URL = urljoin(SOURCE_URL, 'concert-season/')
SOURCE = 'Boston Early Music Festival'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'(January|February|March|April|May|June|July|August|September|October|'
    r'November|December)\s+(\d{1,2}),\s+(20\d{2})\s+at\s+'
    r'(\d{1,2})(?::(\d{2}))?\s*([ap]m)',
    re.I,
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_urls(session):
    response = session.get(SEASON_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    urls = set()
    for link in soup.select('article a[href]'):
        if not clean_text(link).upper().startswith('LEARN MORE'):
            continue
        url = urljoin(SEASON_URL, link.get('href'))
        path = urlparse(url).path
        if re.fullmatch(r'/concert-season/[^/]+/', path):
            urls.add(url)
    return sorted(urls)


def parse_date_time(match):
    month, day, year, hour, minute, meridiem = match.groups()
    try:
        value = datetime.strptime(
            f'{month} {day} {year} {hour}:{minute or "00"} {meridiem.upper()}',
            '%B %d %Y %I:%M %p',
        )
    except ValueError:
        return None, None
    return value.date().isoformat(), value.strftime('%H:%M')


def venue_and_city(value):
    value = clean_text(value)
    if not value:
        return '', ''

    # This venue's official name contains a comma after the city name.
    if re.search(r'First Church in Cambridge', value, re.I):
        return value, 'Cambridge'

    match = re.match(r'^(.*?),\s*([^,]+?)(?:,\s*[A-Z]{2})?$', value)
    if not match:
        return '', ''
    venue, city = (part.strip() for part in match.groups())
    return venue, city


def detail_records(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    article = soup.select_one('article')
    if not article:
        return []

    title_node = article.select_one('h1.entry-title')
    title = clean_text(title_node)
    if 'chamber opera series' in title.lower():
        production = next(
            (clean_text(node) for node in article.select('h2') if 'presents' in clean_text(node).lower()),
            '',
        )
        if production:
            title = re.sub(r'^.*?\bpresents\s+', '', production, flags=re.I).strip()

    description_node = article.select_one('.entry-content') or article
    description = clean_text(description_node) or None
    records = []
    for schedule in article.select('h4'):
        text = clean_text(schedule).replace('\n', ' ')
        if 'VIRTUAL' in text.upper():
            continue
        matches = list(DATE_TIME_RE.finditer(text))
        if not matches:
            continue
        venue_link = schedule.select_one('a[href*="/concert-season/concert-venues/"]')
        venue_text = clean_text(venue_link)
        if not venue_text:
            venue_text = DATE_TIME_RE.sub(' ', text)
            venue_text = re.sub(r'\|?\s*PURCHASE TICKETS\s*»?', ' ', venue_text, flags=re.I)
            venue_text = re.sub(r'\s+', ' ', venue_text).strip(' |')
        venue, city = venue_and_city(venue_text)
        if not title or not venue or not city:
            continue
        for match in matches:
            date, time_from = parse_date_time(match)
            if not date:
                continue
            records.append({
                'title': title,
                'date': date,
                'url': response.url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url in event_urls(session):
        try:
            records.extend(detail_records(session, url))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape concert detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    if not records:
        log_message(
            'No valid concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=SEASON_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class BemfOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bemf_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    BemfOrgCrawler().run()


if __name__ == '__main__':
    main()
