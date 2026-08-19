import re
from datetime import datetime
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.paintedskyopera.org/'
SOURCE = 'Painted Sky Opera'
CALENDAR_URL = urljoin(SOURCE_URL, 'new-events')
DEFAULT_CITY = 'Oklahoma City'
TIME_ZONE = ZoneInfo('America/Chicago')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}
MONTHS = {
    name: number for number, name in enumerate(
        ('january', 'february', 'march', 'april', 'may', 'june',
         'july', 'august', 'september', 'october', 'november', 'december'),
        1,
    )
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def city_from_location(location):
    address = clean_text(location.get('addressLine2'))
    match = re.match(r'([^,]+),\s*[A-Za-z]{2}\b', address)
    if match:
        return match.group(1).strip()

    venue = clean_text(location.get('addressTitle'))
    known = {
        'enid symphony': 'Enid',
        'cashion': 'Cashion',
        'the veraden': 'Edmond',
        'saint andrews episcopal church': 'Lawton',
    }
    venue_lower = venue.lower()
    for fragment, city in known.items():
        if fragment in venue_lower:
            return city
    # The remaining calendar locations are named Oklahoma City institutions.
    return DEFAULT_CITY if venue else None


def calendar_record(item, detail):
    location = item.get('location') or {}
    title = clean_text(item.get('title'))
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)
    path = item.get('fullUrl')
    timestamp = item.get('startDate')
    if not all((title, venue, city, path, timestamp)):
        return None

    start = datetime.fromtimestamp(timestamp / 1000, tz=TIME_ZONE)
    description = clean_text(
        detail.get('body') or detail.get('excerpt') or item.get('body') or item.get('excerpt')
    )
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, path),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_calendar(session):
    records = []
    seen_urls = set()
    page_url = f'{CALENDAR_URL}?format=json'
    seen_pages = set()

    while page_url not in seen_pages:
        seen_pages.add(page_url)
        payload = get_response(session, page_url).json()
        items = [*(payload.get('upcoming') or []), *(payload.get('past') or [])]
        for item in items:
            path = item.get('fullUrl')
            if not path or path in seen_urls:
                continue
            seen_urls.add(path)
            detail_url = f'{urljoin(SOURCE_URL, path)}?format=json'
            try:
                detail = get_response(session, detail_url).json().get('item') or {}
                record = calendar_record(item, detail)
                if record:
                    records.append(record)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Painted Sky Opera calendar detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=detail_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        next_path = (payload.get('pagination') or {}).get('nextPageUrl')
        if not next_path:
            break
        separator = '&' if '?' in next_path else '?'
        page_url = f'{urljoin(SOURCE_URL, next_path)}{separator}format=json'
    return records


def season_page(session):
    soup = BeautifulSoup(get_response(session, SOURCE_URL).text, 'html.parser')
    candidates = []
    for link in soup.select('a[href]'):
        label = clean_text(link).lower()
        href = urljoin(SOURCE_URL, link.get('href', ''))
        path = urlparse(href).path.lower()
        if (
            ('buy ticket' in label or 'buy-ticket' in path)
            and urlparse(href).netloc == urlparse(SOURCE_URL).netloc
        ):
            candidates.append(href)
    for url in dict.fromkeys(candidates):
        response = get_response(session, f'{url}?format=json')
        payload = response.json()
        text = clean_text(payload.get('mainContent'))
        year_match = re.search(
            r'\b(?:Season\s+(20\d{2})|(20\d{2})\s+Season)\b', text, re.I
        )
        if year_match:
            year = year_match.group(1) or year_match.group(2)
            return url, int(year), BeautifulSoup(payload['mainContent'], 'html.parser')
    return None, None, None


def parse_time(hour, minute, meridiem):
    hour = int(hour)
    if meridiem.lower() == 'pm' and hour != 12:
        hour += 12
    if meridiem.lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{int(minute or 0):02d}'


def dated_occurrences(text, year):
    normalized = re.sub(r'\s+', ' ', text)
    matches = list(re.finditer(
        r'\b(' + '|'.join(MONTHS) + r')\s+'
        r'([0-9, &and]+?)\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b',
        normalized,
        re.I,
    ))
    occurrences = []
    for match in matches:
        month = MONTHS[match.group(1).lower()]
        times = [parse_time(match.group(3), match.group(4), match.group(5))]
        second_time = re.match(
            r'\s+and\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b',
            normalized[match.end():],
            re.I,
        )
        if second_time:
            times.append(parse_time(*second_time.groups()))
        days = re.findall(r'\d{1,2}', match.group(2))
        for day_text in days:
            try:
                value = datetime(year, month, int(day_text)).date().isoformat()
            except ValueError:
                continue
            occurrences.extend((value, time_from) for time_from in times)
    return occurrences


def page_record_data(text):
    venue_match = re.search(
        r'\b(THE LITTLE THEATRE at CIVIC CENTER MUSIC HALL)\b', text, re.I
    )
    city_match = re.search(r'\bOKLAHOMA CITY,\s*OKLAHOMA\b', text, re.I)
    if not venue_match or not city_match:
        return None, None
    return 'The Little Theatre at Civic Center Music Hall', DEFAULT_CITY


def scrape_current_season(session):
    season_url, year, season_soup = season_page(session)
    if not season_soup:
        return []

    records = []
    links = []
    for link in season_soup.select('a[href]'):
        url = urljoin(season_url, link.get('href', ''))
        if urlparse(url).netloc == urlparse(SOURCE_URL).netloc and url != season_url:
            links.append(url)

    for url in dict.fromkeys(links):
        try:
            payload = get_response(session, f'{url}?format=json').json()
            soup = BeautifulSoup(payload.get('mainContent') or '', 'html.parser')
            text = clean_text(soup)
            occurrences = dated_occurrences(text[:1500], year)
            venue, city = page_record_data(text[:1500])
            heading = soup.select_one('h1')
            title = clean_text(heading).split('||', 1)[0].strip()
            if not all((title, venue, city)) or not occurrences:
                continue
            description = text or None
            for event_date, time_from in occurrences:
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
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to scrape Painted Sky Opera season detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = [*scrape_calendar(session), *scrape_current_season(session)]
    unique = {
        (record['url'], record['date'], record['time_from']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class PaintedSkyOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='paintedskyopera_org',
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
        return get_concerts()


def main():
    PaintedSkyOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
