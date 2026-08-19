import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mso.org/'
CALENDAR_URL = f'{SOURCE_URL}concerts/calendar/'
SOURCE = 'Milwaukee Symphony Orchestra'
DEFAULT_VENUE = 'Bradley Symphony Center'
DEFAULT_CITY = 'Milwaukee'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {month.lower(): number for number, month in enumerate(
    ('January', 'February', 'March', 'April', 'May', 'June',
     'July', 'August', 'September', 'October', 'November', 'December'),
    start=1,
)}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def month_from_url(url):
    match = re.search(r'/calendar/(\d{4})/([a-z]+)/?', url, re.I)
    if not match or match.group(2).lower() not in MONTHS:
        return None
    return int(match.group(1)), MONTHS[match.group(2).lower()]


def parse_time(value):
    value = clean_text(value).lower().replace('.', '').replace(' ', '')
    for pattern in ('%I:%M%p', '%I%p', '%H:%M'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def canonical_detail_url(url):
    parts = urlsplit(url)
    path = re.sub(r'/\d+/?$', '/', parts.path)
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def parse_location(detail_soup):
    range_node = detail_soup.select_one('.performance-range')
    range_text = clean_text(range_node).replace('\n', ' ')
    match = re.search(r'\s+at\s+(.*)$', range_text, re.I)
    if not match:
        return DEFAULT_VENUE, DEFAULT_CITY

    location = match.group(1).strip(' ,')
    parts = [part.strip() for part in location.split(',') if part.strip()]
    if len(parts) < 2:
        return (location, DEFAULT_CITY) if location else (DEFAULT_VENUE, DEFAULT_CITY)

    venue = parts[0]
    city = re.sub(r'\s+[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?$', '', parts[-1]).strip()
    if not city or re.search(r'\d', city):
        city = DEFAULT_CITY
    return venue, city


def detail_data(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    venue, city = parse_location(soup)
    main = soup.select_one('main')
    description = clean_text(main)
    return venue, city, description or None


def calendar_urls(session):
    response = session.get(CALENDAR_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    urls = {response.url}
    for link in soup.select('a[href*="/concerts/calendar/"]'):
        url = urljoin(response.url, link.get('href'))
        if month_from_url(url):
            urls.add(url.split('#', 1)[0])
    for option in soup.select('#month_switcher option[value]'):
        url = urljoin(response.url, option.get('value'))
        if month_from_url(url):
            urls.add(url)
    return sorted(urls, key=lambda url: month_from_url(url) or (0, 0))


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    detail_cache = {}
    records = []

    for calendar_url in calendar_urls(session):
        year_month = month_from_url(calendar_url)
        if not year_month:
            continue
        year, month = year_month
        response = session.get(calendar_url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        for event in soup.select('.js-filterable-event.mot-calendar-event'):
            day_node = event.find_parent('li', class_='active-date')
            title_link = event.select_one('.show-title a[href]')
            day_indicator = day_node.select_one('.day-indicator') if day_node else None
            if not day_node or not title_link or not day_indicator:
                continue

            title = clean_text(title_link)
            try:
                event_date = datetime(year, month, int(clean_text(day_indicator))).date().isoformat()
            except (TypeError, ValueError):
                continue
            url = urljoin(response.url, title_link.get('href'))
            time_from = parse_time(event.select_one('.event-time'))
            cache_key = canonical_detail_url(url)
            if cache_key not in detail_cache:
                try:
                    detail_cache[cache_key] = detail_data(session, url)
                except requests.RequestException as error:
                    log_message(
                        'Concert detail request failed',
                        event='crawler_detail_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
            venue, city, description = detail_cache[cache_key]
            if not title or not venue or not city:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    unique = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        unique[key] = record
    return sorted(unique.values(), key=lambda item: (
        item['date'], item['time_from'] or '', item['title']
    ))


class MsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mso_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    MsoOrgCrawler().run()


if __name__ == '__main__':
    main()
