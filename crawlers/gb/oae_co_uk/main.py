import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://oae.co.uk/'
SOURCE = 'Orchestra of the Age of Enlightenment'
EVENTS_API = f'{SOURCE_URL}wp-json/wp/v2/event'
WHATS_ON_API = f'{SOURCE_URL}wp-content/uploads/whats-on.json'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}
DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+'
    r'(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|'
    r'September|October|November|December)\s+20\d{2})'
    r'(?:,\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?',
    re.IGNORECASE,
)

CITY_COUNTRIES = {
    'Amsterdam': 'NL', 'Antwerp': 'BE', 'Basingstoke': 'GB', 'Bedford': 'GB',
    'Berkeley': 'US', 'Bradford-on-Avon': 'GB', 'Bradford on Avon': 'GB',
    'Brighton': 'GB', 'Brno': 'CZ', 'Bucharest': 'RO', 'Budapest': 'HU',
    'Cambridge': 'GB', 'Darlington': 'GB', 'Dortmund': 'DE', 'Dublin': 'IE',
    'Edinburgh': 'GB', 'Frankfurt': 'DE', 'Great Malvern': 'GB', 'Halle': 'DE',
    'Hamburg': 'DE', 'Lewes': 'GB', 'Linz': 'AT', 'London': 'GB', 'Lucerne': 'CH',
    'Lugano': 'CH', 'Manchester': 'GB', 'Munich': 'DE', 'New York': 'US',
    'Norton': 'GB', 'Oxford': 'GB', 'Portsmouth': 'GB', 'Prague': 'CZ',
    'Santa Barbara': 'US', 'Scarborough': 'GB', 'Sheffield': 'GB',
    'Saffron Walden': 'GB', 'Tetbury': 'GB', 'Udine': 'IT', 'Vienna': 'AT',
    'Warwick': 'GB', 'Washington DC': 'US', 'Wimbledon': 'GB', 'York': 'GB',
    'Zürich': 'CH',
}
LONDON_AREAS = {
    'Brixton', 'Chalk Farm', 'Hampstead', 'Islington', "King's Cross", 'Kings Cross',
    'Shadwell', 'Tufnell Park',
}
LONDON_VENUE_MARKERS = (
    'acland burghley', 'artists’ bar', "artists' bar", 'backstage bar',
    'brasserie blanc', 'côte brasserie', 'côte royal festival hall',
    'fairfield halls', 'hayward gallery', 'henry wood hall', 'kings place',
    'lasdun restaurant', 'national theatre', 'ognisko restaurant',
    'queen elizabeth hall', 'royal academy of music', 'royal festival hall',
    'southbank centre', 'the ninth', 'the pineapple',
)


def clean_text(value, separator=' '):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(separator, strip=True)
    text = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount(
        'https://',
        HTTPAdapter(
            max_retries=Retry(
                total=2,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=('GET',),
            )
        ),
    )
    return session


def parse_time(value):
    value = clean_text(value).lower().replace(' ', '')
    match = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?(am|pm)?', value)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = match.group(3)
    if minute > 59 or (suffix and hour not in range(1, 13)) or (not suffix and hour > 23):
        return None
    if suffix == 'pm' and hour != 12:
        hour += 12
    elif suffix == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def parse_location(value):
    value = clean_text(value)
    parts = [part.strip() for part in value.split(',') if part.strip()]
    for index, part in enumerate(parts):
        if part in CITY_COUNTRIES:
            venue = ', '.join(parts[:index] + parts[index + 1:])
            return part, venue or None, CITY_COUNTRIES[part]
        if part in LONDON_AREAS:
            venue = ', '.join(parts[:index] + parts[index + 1:])
            return 'London', venue or None, 'GB'
    if '.' in value:
        # This form is used for OAE's London pub concerts: the first part is
        # a London neighbourhood rather than a city (for example King's Cross).
        _, venue = (part.strip() for part in value.split('.', 1))
        return 'London', venue or None, 'GB'
    if any(marker in value.casefold() for marker in LONDON_VENUE_MARKERS):
        return 'London', value or None, 'GB'
    return None, None, None


def page_description(soup):
    parts = []
    summary = soup.select_one('meta[name="description"]')
    summary_text = clean_text(summary.get('content')) if summary else ''
    if summary_text:
        parts.append(summary_text)

    details = soup.select_one('.event-header__details[data-display-size="desktop"]')
    if not details:
        details = soup.select_one('.event-header__details')
    if details:
        for item in details.select('.event-header__list-item'):
            text = clean_text(item, separator='\n')
            if text and text not in parts:
                parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event_page(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    article = soup.select_one('article')
    if not article:
        return []
    title = clean_text(article.select_one('h1'))
    location = clean_text(article.select_one('.event-header__location-title'))
    city, venue, country_code = parse_location(location)
    description = page_description(soup)
    records = []
    seen_dates = set()
    for node in article.select('.event-header__date'):
        for match in DATE_RE.finditer(clean_text(node)):
            try:
                event_date = datetime.strptime(match.group(1), '%d %B %Y').date().isoformat()
            except ValueError:
                continue
            time_from = parse_time(match.group(2)) if match.group(2) else None
            key = (event_date, time_from)
            if key in seen_dates:
                continue
            seen_dates.add(key)
            if title and city and venue:
                records.append(
                    make_record(
                        title, event_date, time_from, venue, city, url, description, country_code
                    )
                )
    return records


def make_record(title, event_date, time_from, venue, city, url, description, country_code='GB'):
    return {
        'title': clean_text(title),
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': clean_text(venue),
        'city': clean_text(city),
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def current_records(session):
    response = session.get(WHATS_ON_API, timeout=45)
    response.raise_for_status()
    records = []
    for card in response.json().get('cards', []):
        location = card.get('overriden_location') or card.get('full_location')
        city, venue, country_code = parse_location(location)
        title = clean_text(card.get('name'))
        url = card.get('url')
        if not title or not url or not city or not venue:
            continue
        description = clean_text(card.get('summary')) or None
        for instance in card.get('instances') or []:
            try:
                timestamp = datetime.strptime(instance.get('date', ''), '%Y-%m-%d %H:%M:%S')
            except (TypeError, ValueError):
                continue
            records.append(
                make_record(
                    title,
                    timestamp.date().isoformat(),
                    timestamp.strftime('%H:%M'),
                    venue,
                    city,
                    url,
                    description,
                    country_code,
                )
            )
    return records


def event_urls(session):
    urls = []
    page = 1
    while True:
        response = session.get(
            EVENTS_API,
            params={'per_page': 100, 'page': page, '_fields': 'link'},
            timeout=45,
        )
        if response.status_code == 400 and page > 1:
            break
        response.raise_for_status()
        items = response.json()
        urls.extend(item.get('link') for item in items if item.get('link'))
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1
    return list(dict.fromkeys(urls))


def get_concerts():
    session = make_session()
    records = current_records(session)
    urls = event_urls(session)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(session.get, url, timeout=45): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                response = future.result()
                response.raise_for_status()
                records.extend(parse_event_page(response.content, url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape OAE event page',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class OaeCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='oae_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OaeCoUkCrawler().run()


if __name__ == '__main__':
    main()
