import html
import json
import re
from datetime import date

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sdjsymfoni.dk/'
SOURCE = 'Sønderjyllands Symfoniorkester'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/mec-events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'da-DK,da;q=0.9,en;q=0.7',
}

DANISH_MONTHS = {
    'januar': 1,
    'februar': 2,
    'marts': 3,
    'april': 4,
    'maj': 5,
    'juni': 6,
    'juli': 7,
    'august': 8,
    'september': 9,
    'oktober': 10,
    'november': 11,
    'december': 12,
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def event_json_ld(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        entries = payload if isinstance(payload, list) else [payload]
        for entry in entries:
            if isinstance(entry, dict) and entry.get('@type') == 'Event':
                return entry
    return None


def parse_city_country(address):
    address = clean_text(address)
    country_code = 'DE' if re.search(r'\b(?:Tyskland|Deutschland|Germany)\b', address, re.I) else 'DK'
    match = re.search(r'\b\d{4,5}\s+([^,]+)', address)
    if not match:
        return None, country_code
    city = match.group(1).strip(' ,-')
    return (city or None), country_code


def parse_clock(value):
    match = re.search(r'\b([01]?\d|2[0-3])[:.]([0-5]\d)\b', clean_text(value))
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2)}'


def description_occurrence_times(description, event_date):
    """Find multiple explicitly advertised times when MEC omitted its time field."""
    month_name = next(name for name, number in DANISH_MONTHS.items() if number == event_date.month)
    pattern = re.compile(
        rf'\b{event_date.day}\.\s*{month_name}\b[^\n.]{{0,35}}?'
        r'kl\.\s*([0-2]?\d[.:][0-5]\d)'
        r'(?:\s*(?:og|&)\s*([0-2]?\d[.:][0-5]\d))?',
        re.IGNORECASE,
    )
    times = []
    for match in pattern.finditer(description):
        for value in match.groups():
            parsed = parse_clock(value)
            if parsed and parsed not in times:
                times.append(parsed)
    return times


def parse_detail(soup, url):
    schema = event_json_ld(soup)
    if not schema:
        return []

    title = clean_text(soup.select_one('.mec-single-title')) or clean_text(schema.get('name'))
    location = schema.get('location') if isinstance(schema.get('location'), dict) else {}
    venue = clean_text(location.get('name'))
    city, country_code = parse_city_country(location.get('address'))
    try:
        event_date = date.fromisoformat(str(schema.get('startDate', ''))[:10])
    except ValueError:
        return []

    description = clean_text(soup.select_one('.mec-single-event-description'))
    if not description:
        description = clean_text(schema.get('description')) or None

    time_box = soup.select_one('.mec-single-event-time')
    times = [parse_clock(time_box)] if time_box else []
    times = [value for value in times if value]
    if not times and description:
        times = description_occurrence_times(description, event_date)

    # MEC is also used for ticket packages and concert-series overview pages.
    # Those pages have synthetic dates but no occurrence time and must not be
    # emitted as concrete concerts.
    if not title or not venue or not city or not times:
        return []

    base = {
        'title': title,
        'date': event_date.isoformat(),
        'url': url,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }
    return [{**base, 'time_from': event_time} for event_time in times]


def build_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


class SdjsymfoniDkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sdjsymfoni_dk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DK',
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
        session = build_session()
        posts = []
        page = 1
        while True:
            try:
                response = session.get(
                    API_URL,
                    params={
                        'per_page': 100,
                        'page': page,
                        'orderby': 'id',
                        'order': 'asc',
                        '_fields': 'id,link',
                    },
                    timeout=45,
                )
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Sønderjyllands Symfoniorkester event index',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            batch = response.json()
            posts.extend(batch)
            total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            if page >= total_pages:
                break
            page += 1

        records = []
        for post in posts:
            url = post.get('link')
            if not url:
                continue
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch symphony event detail',
                    event='crawler_item_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            records.extend(parse_detail(BeautifulSoup(response.text, 'html.parser'), url))

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'], record['title'], record['venue']
            ),
        )


def main():
    SdjsymfoniDkCrawler().run()


if __name__ == '__main__':
    main()
