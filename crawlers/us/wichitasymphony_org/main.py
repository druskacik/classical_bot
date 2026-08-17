import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://wichitasymphony.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
SOURCE = 'Wichita Symphony Orchestra'
CITY = 'Wichita'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

SEASON_RE = re.compile(r'\b(20\d{2})\s*[-–]\s*(20\d{2})\b')


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    value = clean_text(value).upper().replace('.', '')
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_event_date(value, category='', today=None):
    """Resolve the listing's month/day against its displayed season or today."""
    try:
        partial = datetime.strptime(clean_text(value), '%b %d')
    except ValueError:
        return ''

    season = SEASON_RE.search(clean_text(category))
    if season:
        start_year, end_year = map(int, season.groups())
        year = start_year if partial.month >= 7 else end_year
    else:
        today = today or date.today()
        candidates = [
            date(year, partial.month, partial.day)
            for year in (today.year - 1, today.year, today.year + 1)
        ]
        year = min(candidates, key=lambda item: abs((item - today).days)).year

    try:
        return date(year, partial.month, partial.day).isoformat()
    except ValueError:
        return ''


def fetch_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def detail_data(session, url):
    soup = fetch_soup(session, url)
    venue = clean_text(soup.select_one('.overview .location'))
    description_node = soup.select_one('.event_description')
    if description_node:
        for node in description_node.select('script, style, iframe'):
            node.decompose()
    return venue, clean_text(description_node) or None


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    soup = fetch_soup(session, EVENTS_URL)
    records = []
    season_option = soup.select_one('.event_filter option[value*="season"]')
    listing_season = clean_text(season_option)

    for card in soup.select('ol.events > li'):
        title_node = card.select_one('.event_name .title')
        link = title_node.find('a', href=True) if title_node else None
        title = clean_text(title_node)
        url = urljoin(EVENTS_URL, link['href']) if link else ''
        category = clean_text(card.select_one('.event_name .category'))
        schedules = card.select('.schedule .date_time')
        if not title or not url or not schedules:
            continue

        try:
            venue, description = detail_data(session, url)
        except requests.RequestException as error:
            log_message(
                'Event detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if not venue:
            continue

        for schedule in schedules:
            event_date = parse_event_date(
                clean_text(schedule.select_one('.date')),
                f'{category} {listing_season}',
            )
            if not event_date:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': parse_time(schedule.select_one('.time')),
                'venue': venue,
                'city': CITY,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    if not records:
        log_message(
            'No valid event records found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class WichitaSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wichitasymphony_org',
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
        return scrape_events()


def main():
    WichitaSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
