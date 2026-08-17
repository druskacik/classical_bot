import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.baroque.org/'
SEASONS_URL = urljoin(SOURCE_URL, 'Seasons')
SOURCE = 'Music of the Baroque'
FIRST_ARCHIVED_SEASON = 2014

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(r'([A-Z][a-z]+\s+\d{1,2},\s+\d{4})')
TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?\s*[AP]M)\b', re.I)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(match.group(1).upper(), pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def season_urls(session):
    response = session.get(SEASONS_URL, timeout=45)
    response.raise_for_status()
    match = re.search(r'/Seasons/(\d{4})-(\d{4})(?:/|$)', response.url)
    if not match:
        raise ValueError(f'Could not determine active season from {response.url}')
    active_start = int(match.group(1))
    return [
        f'{SEASONS_URL}/{year}-{year + 1}'
        for year in range(FIRST_ARCHIVED_SEASON, active_start + 1)
    ]


def detail_urls_from_season(soup, season_url):
    season_path = urlparse(season_url).path.rstrip('/') + '/'
    urls = set()
    for link in soup.select('a[href]'):
        url = urljoin(season_url, link.get('href'))
        if urlparse(url).path.startswith(season_path) and urlparse(url).path != season_path:
            urls.add(url.split('#', 1)[0])
    return sorted(urls)


def parse_detail(soup, url):
    title = re.sub(r'\s+', ' ', clean_text(soup.select_one('h1'))).strip()
    if not title:
        return []

    description_parts = []
    for selector in ('.program', '.description'):
        text = clean_text(soup.select_one(selector))
        if text and text not in description_parts:
            description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    records = []
    for occurrence in soup.select('li.sb-content-concert'):
        venue_link = occurrence.select_one('a[href*="/Venues/"]')
        if not venue_link:
            continue
        location = clean_text(venue_link).rstrip(' »').strip()
        if not location or location.lower().startswith('on demand') or ',' not in location:
            continue
        venue, city = [part.strip() for part in location.rsplit(',', 1)]
        event_date = parse_date(occurrence.select_one('h2'))
        if not event_date or not venue or not city:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(occurrence.select_one('h2')),
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
    detail_urls = set()
    for season_url in season_urls(session):
        response = session.get(season_url, timeout=45)
        response.raise_for_status()
        if response.url.rstrip('/') != season_url.rstrip('/'):
            continue
        detail_urls.update(detail_urls_from_season(BeautifulSoup(response.text, 'html.parser'), season_url))

    records = []
    for url in sorted(detail_urls):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            records.extend(parse_detail(BeautifulSoup(response.text, 'html.parser'), response.url))
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=SEASONS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class BaroqueOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='baroque_org',
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
    BaroqueOrgCrawler().run()


if __name__ == '__main__':
    main()
