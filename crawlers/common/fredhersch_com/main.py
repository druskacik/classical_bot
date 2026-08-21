import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://fredhersch.com/'
TOUR_URL = urljoin(SOURCE_URL, 'tour/')
API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/tour')
SOURCE = 'Fred Hersch'
PER_PAGE = 100

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/json',
}

COUNTRY_CODES = {
    'argentina': 'AR', 'australia': 'AU', 'austria': 'AT', 'au': 'AT',
    'be': 'BE', 'belgium': 'BE', 'brazil': 'BR', 'ca': 'CA', 'canada': 'CA',
    'ch': 'CH', 'china': 'CN', 'croatia': 'HR', 'czech republic': 'CZ',
    'de': 'DE', 'denmark': 'DK', 'finland': 'FI', 'france': 'FR',
    'germany': 'DE', 'greece': 'GR', 'hungary': 'HU', 'ireland': 'IE',
    'israel': 'IL', 'italy': 'IT', 'japan': 'JP', 'mexico': 'MX',
    'netherlands': 'NL', 'norway': 'NO', 'poland': 'PL', 'portugal': 'PT',
    'romania': 'RO', 'south korea': 'KR', 'spain': 'ES', 'sweden': 'SE',
    'switzerland': 'CH', 'turkey': 'TR', 'uk': 'GB', 'united kingdom': 'GB',
    'us': 'US', 'usa': 'US', 'united states': 'US',
}
US_STATE = re.compile(r'^[A-Z]{2}$')
US_STATE_NAMES = {
    'alabama', 'alaska', 'arizona', 'arkansas', 'california', 'colorado',
    'connecticut', 'delaware', 'florida', 'georgia', 'hawaii', 'idaho',
    'illinois', 'indiana', 'iowa', 'kansas', 'kentucky', 'louisiana', 'maine',
    'maryland', 'massachusetts', 'michigan', 'minnesota', 'mississippi',
    'missouri', 'montana', 'nebraska', 'nevada', 'new hampshire', 'new jersey',
    'new mexico', 'new york', 'north carolina', 'north dakota', 'ohio',
    'oklahoma', 'oregon', 'pennsylvania', 'rhode island', 'south carolina',
    'south dakota', 'tennessee', 'texas', 'utah', 'vermont', 'virginia',
    'washington', 'west virginia', 'wisconsin', 'wyoming',
    'alberta', 'british columbia', 'manitoba', 'new brunswick',
    'newfoundland and labrador', 'nova scotia', 'ontario',
    'prince edward island', 'quebec', 'saskatchewan',
}
TIME = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b', re.I)


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r' *\n+ *', '\n', text).strip()


def parse_time(value):
    match = TIME.search(value or '')
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def parse_location(item):
    location = item.select_one('.event-city')
    if not location:
        return None
    country_node = location.select_one('.event-country')
    country_label = clean_text(country_node.get_text(' ', strip=True) if country_node else '')
    country_code = COUNTRY_CODES.get(country_label.lower())
    if not country_code:
        return None

    if country_node:
        country_node.extract()
    location_text = re.sub(r'\s+', ' ', location.get_text(' ', strip=True)).strip().rstrip(' -')
    parts = [part.strip(' ,-') for part in location_text.split(',') if part.strip(' ,-')]
    if not parts:
        return None

    if len(parts) >= 2 and parts[-1].lower() == country_label.lower():
        parts.pop()
        if not parts:
            return None

    venue = None
    is_state = US_STATE.fullmatch(parts[-1]) or parts[-1].lower() in US_STATE_NAMES
    if country_code in {'US', 'CA'} and len(parts) >= 2 and is_state:
        city = parts[-2]
        if len(parts) >= 3:
            venue = ', '.join(parts[:-2])
    else:
        city = parts[-1]
        if len(parts) >= 2:
            venue = ', '.join(parts[:-1])
    return city, country_code, venue


def record_from_html(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    item = soup.select_one('#tour-page .item')
    if not item:
        return None

    date_node = item.select_one('.event-date')
    date_text = clean_text(date_node.get_text('\n', strip=True) if date_node else '').split('\n')[0]
    try:
        event_date = datetime.strptime(date_text, '%d %b %Y').date().isoformat()
    except ValueError:
        return None
    if event_date == '1970-01-01':
        return None

    location = parse_location(item)
    if not location:
        return None
    city, country_code, location_venue = location

    billing = clean_text(item.select_one('.event-venue'))
    if not billing:
        return None
    billing_parts = [part.strip() for part in billing.split(' - ', 1)]
    if len(billing_parts) == 2:
        venue, title = billing_parts
    else:
        venue, title = location_venue, billing
    if not venue:
        return None

    details = clean_text(item.select_one('.event-details'))
    info = clean_text(item.select_one('.event-info'))
    notes = clean_text(soup.select_one('#tour-page .event-notes'))
    description_parts = [part for part in (details, notes) if part]

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time('\n'.join((clean_text(date_node), details, info))),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event_urls(session):
    urls = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': PER_PAGE, 'page': page, '_fields': 'link'},
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        urls.extend(item['link'] for item in payload if item.get('link'))
        total_pages = int(response.headers.get('X-WP-TotalPages') or page)
        if page >= total_pages:
            break
        page += 1
    return urls


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    urls = fetch_event_urls(session)
    records = []
    for url in urls:
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            record = record_from_html(response.text, url)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Fred Hersch event detail',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue'], record['url']): record
        for record in records
    }
    result = sorted(unique.values(), key=lambda record: (
        record['date'], record['time_from'] or '', record['title'], record['venue']
    ))
    if not result:
        log_message(
            'No valid Fred Hersch tour events found',
            event='crawler_empty_listing',
            level='warning',
            url=TOUR_URL,
            record_count=0,
        )
    return result


class FredHerschComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fredhersch_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'url'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    FredHerschComCrawler().run()


if __name__ == '__main__':
    main()
