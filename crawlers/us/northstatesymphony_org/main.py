import re
from datetime import datetime
from html import unescape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://northstatesymphony.org/'
SOURCE = 'North State Symphony'
SEASON_URL = urljoin(SOURCE_URL, 'current-season/')
EVENTS_API_URL = urljoin(SOURCE_URL, 'wp-json/tribe/events/v1/events')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
    'Connection': 'close',
}

DATE_LINE_RE = re.compile(
    r'(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'(?P<day>\d{1,2})',
    re.IGNORECASE,
)
DETAIL_RE = re.compile(
    r'(?P<time>\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?))\s*\|\s*'
    r'(?P<venue>[^,\n|]+),\s*(?P<city>[^\n|]+)',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    else:
        value = unescape(str(value))
        if '<' in value and '>' in value:
            value = BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
    value = value.replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def parse_time(value):
    normalized = re.sub(r'\.', '', clean_text(value)).upper()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def season_year(soup):
    modified = soup.select_one('meta[property="article:modified_time"]')
    if modified:
        match = re.match(r'(\d{4})', modified.get('content', ''))
        if match:
            return int(match.group(1))

    upload_years = [int(year) for year in re.findall(r'/uploads/(20\d{2})/', str(soup))]
    return max(upload_years) if upload_years else datetime.now().year


def occurrence_date(month, day, start_year):
    month_number = datetime.strptime(month, '%B').month
    year = start_year + (1 if month_number <= 6 else 0)
    try:
        return datetime(year, month_number, int(day)).date().isoformat()
    except ValueError:
        return None


def get(session, url, **kwargs):
    last_error = None
    for _ in range(3):
        try:
            response = session.get(url, timeout=45, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            last_error = error
    raise last_error


def scrape_season(session):
    response = get(session, SEASON_URL)
    soup = BeautifulSoup(response.text, 'html.parser')
    main = soup.select_one('main')
    if not main:
        return []

    year = season_year(soup)
    programme_links = []
    for link in main.select('strong > a[href]'):
        url = urljoin(SEASON_URL, link.get('href'))
        if url.startswith(SOURCE_URL) and url not in programme_links:
            programme_links.append(url)

    records = []
    for url in programme_links:
        detail = BeautifulSoup(get(session, url).text, 'html.parser')
        detail_main = detail.select_one('main')
        if not detail_main:
            continue
        description = clean_text(detail_main)
        title_node = detail.select_one('h1.entry-title, main h1')
        title = clean_text(title_node)
        if not title:
            title = clean_text(detail.title).removesuffix(' - North State Symphony')

        lines = [line for line in description.splitlines() if line]
        for index, line in enumerate(lines):
            date_text = line
            if re.fullmatch(
                r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?',
                line,
                re.I,
            ) and index + 1 < len(lines):
                date_text = f'{line} {lines[index + 1]}'
            date_match = DATE_LINE_RE.fullmatch(date_text)
            if not date_match:
                continue
            details = None
            for candidate in lines[index + 1:index + 4]:
                details = DETAIL_RE.fullmatch(candidate)
                if details:
                    break
            if not details:
                continue
            event_date = occurrence_date(date_match['month'], date_match['day'], year)
            venue = clean_text(details['venue'])
            city = clean_text(details['city'])
            if not all((title, event_date, venue, city)):
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': parse_time(details['time']),
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def api_venue(event):
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    # Some calendar entries use a street address as the venue name. Prefer the
    # named location stated in titles such as "... at Redding Public Market".
    if re.match(r'^\d+\s+', venue):
        title_location = re.search(r'\s+at\s+(.+)$', clean_text(event.get('title')), re.I)
        venue = clean_text(title_location.group(1)) if title_location else ''
    return venue, city


def scrape_calendar(session):
    records = []
    page = 1
    params = {
        'per_page': 50,
        'start_date': '2000-01-01 00:00:00',
        'end_date': f'{datetime.now().year + 10}-12-31 23:59:59',
        'page': page,
    }
    while True:
        params['page'] = page
        payload = get(session, EVENTS_API_URL, params=params).json()
        for event in payload.get('events', []):
            venue, city = api_venue(event)
            start_value = event.get('start_date', '')
            try:
                start = datetime.strptime(start_value, '%Y-%m-%d %H:%M:%S')
            except (TypeError, ValueError):
                continue
            title = clean_text(event.get('title'))
            url = clean_text(event.get('url'))
            if not all((title, url, venue, city)):
                continue
            records.append({
                'title': title,
                'date': start.date().isoformat(),
                'url': url,
                'time_from': None if event.get('all_day') else start.strftime('%H:%M'),
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': clean_text(event.get('description')) or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages:
            break
        page += 1
    return records


class NorthStateSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='northstatesymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        records = scrape_season(session)
        try:
            records.extend(scrape_calendar(session))
        except requests.RequestException as error:
            log_message(
                'Calendar API request failed; returning season concerts',
                event='crawler_calendar_api_failed',
                level='warning',
                url=EVENTS_API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
                record_count=len(records),
            )
        if not records:
            log_message(
                'No concerts found',
                event='crawler_empty_listing',
                level='warning',
                url=SEASON_URL,
                record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


def main():
    NorthStateSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
