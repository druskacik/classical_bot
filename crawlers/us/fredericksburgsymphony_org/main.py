import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://fredericksburgsymphony.org/'
SOURCE = 'Fredericksburg Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/fso_concert'
SEASON_URL = f'{SOURCE_URL}season/'
DEFAULT_VENUE = 'James Monroe Auditorium'
DEFAULT_CITY = 'Fredericksburg'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True) if '<' in raw else raw
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    value = re.sub(r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+', '', clean_text(value), flags=re.I)
    try:
        return datetime.strptime(value, '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def season_schedule(html):
    """Return date-keyed time and location data from first-party calendar links."""
    soup = BeautifulSoup(html, 'html.parser')
    schedule = {}
    for article in soup.select('article[data-date]'):
        event_date = article.get('data-date', '').strip()
        link = article.select_one('a[href*="calendar.google.com/calendar/render"]')
        if not event_date or not link:
            continue

        query = parse_qs(urlparse(link.get('href', '')).query)
        date_range = query.get('dates', [''])[0]
        start = date_range.split('/', 1)[0]
        time_from = None
        if re.fullmatch(r'\d{8}T\d{6}', start):
            try:
                time_from = datetime.strptime(start, '%Y%m%dT%H%M%S').strftime('%H:%M')
            except ValueError:
                pass

        location = clean_text(query.get('location', [''])[0])
        venue = clean_text(location.split(',', 1)[0]) or DEFAULT_VENUE
        city = DEFAULT_CITY if re.search(r'\bFredericksburg\b', location, re.I) else ''
        schedule[event_date] = {
            'time_from': time_from,
            'venue': venue,
            'city': city or DEFAULT_CITY,
        }
    return schedule


def fetch_concerts(session):
    concerts = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page, 'orderby': 'date', 'order': 'asc'},
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        if not isinstance(batch, list):
            raise ValueError('Concert API returned an unexpected response')
        concerts.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return concerts
        page += 1


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    season_response = session.get(SEASON_URL, timeout=45)
    season_response.raise_for_status()
    schedule = season_schedule(season_response.text)

    records = []
    for concert in fetch_concerts(session):
        meta = concert.get('meta') or {}
        event_date = parse_date(meta.get('concert_date'))
        title = clean_text((concert.get('title') or {}).get('rendered'))
        url = clean_text(concert.get('link'))
        if not event_date or not title or not url:
            continue

        details = schedule.get(event_date, {})
        venue = clean_text(details.get('venue') or meta.get('venue')) or DEFAULT_VENUE
        city = clean_text(details.get('city')) or DEFAULT_CITY
        description = clean_text((concert.get('content') or {}).get('rendered')) or None
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': details.get('time_from'),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    if not records:
        log_message(
            'No concert records found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class FredericksburgSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fredericksburgsymphony_org',
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
        return scrape_concerts()


def main():
    FredericksburgSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
