import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://mavzenekar.hu/'
SOURCE = 'MÁV Szimfonikus Zenekar'
UPCOMING_API_URL = urljoin(SOURCE_URL, 'api/hu/concert/upcoming')
PREVIOUS_API_URL = urljoin(SOURCE_URL, 'api/hu/concert/previous')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'hu-HU,hu;q=0.9,en;q=0.7',
}

# These are the venues offered by the site's first-party venue filter. They are
# all in Budapest, even when a detail page happens to omit the address.
BUDAPEST_VENUES = {
    'Budapest Jazz Club',
    'Budapest Music Center, Nagyterem',
    'Eötvös 10',
    'Festetics Palota',
    'Francia Intézet',
    'Magyar Nemzeti Múzeum',
    'MÁV Szimfonikus Zenekar próbaterem',
    'Müpa',
    'Olasz Kultúrintézet',
    'Zeneakadémia',
}

TOUR_LOCATIONS = {
    'bécs': ('Bécs', 'AT'),
    'radiokulturhaus': ('Bécs', 'AT'),
    'salzburg': ('Salzburg', 'AT'),
    'schönbrunn': ('Bécs', 'AT'),
    'peking': ('Peking', 'CN'),
    'beijing': ('Beijing', 'CN'),
    'sanghaj': ('Sanghaj', 'CN'),
    'shanghai': ('Shanghai', 'CN'),
    'xining': ('Xining', 'CN'),
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=4,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def get_json(session, url, params):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def previous_events(session):
    skip = 0
    while True:
        payload = get_json(session, PREVIOUS_API_URL, {'skip': skip})
        data = payload.get('data') or {}
        events = data.get('concerts') or []
        yield from events
        if not events or not data.get('hasMore'):
            break
        skip += len(events)


def upcoming_events(session):
    # Published seasons are normally at most a year ahead. Five years avoids a
    # fragile empty-month stopping rule while keeping the monthly API bounded.
    today = date.today()
    for year in range(today.year, today.year + 6):
        for month in range(1, 13):
            if year == today.year and month < today.month:
                continue
            payload = get_json(
                session,
                UPCOMING_API_URL,
                {'year': year, 'month': month, 'venue': ''},
            )
            yield from payload.get('data') or []


def api_events(session):
    seen = set()
    for event in (*previous_events(session), *upcoming_events(session)):
        slug = clean_text(event.get('slug')).lstrip('/')
        if slug and slug not in seen:
            seen.add(slug)
            yield event


def labelled_value(soup, label):
    for item in soup.select('.concert-info-card .info-item'):
        item_label = clean_text(item.select_one('.info-label')).casefold()
        if label.casefold() in item_label:
            return item.select_one('.info-value')
    return None


def tour_location(title):
    folded = title.casefold()
    for marker, location in TOUR_LOCATIONS.items():
        if marker in folded:
            return location
    return None


def title_venue(title, city):
    # A few archived tour entries lost their venue record, but retain an
    # explicit "city, venue" in the event title.
    match = re.search(r'[-–]\s*([^,]+),\s*(.+)$', title)
    if match and city.casefold() in match.group(1).casefold():
        return clean_text(match.group(2))
    if 'schönbrunn' in title.casefold():
        return 'Schönbrunn Palace'
    if 'mozarteum' in title.casefold():
        return 'Mozarteum'
    if 'radiokulturhaus' in title.casefold():
        return 'Radiokulturhaus'
    return ''


def parse_location(soup, event, title):
    value = labelled_value(soup, 'helyszín')
    venue = ''
    address = ''
    if value:
        small = value.select_one('small')
        address = clean_text(small)
        if small:
            small.extract()
        venue = clean_text(value)
    venue = venue or clean_text(event.get('venue'))

    if address:
        city = clean_text(address.split(',', 1)[0]).strip(' .')
        city = re.sub(r'^\d{4}\s+', '', city)
        if city:
            return venue, city, 'HU'

    tour = tour_location(title)
    if tour:
        city, country_code = tour
        venue = venue or title_venue(title, city)
        return (venue, city, country_code) if venue else None

    if venue in BUDAPEST_VENUES:
        return venue, 'Budapest', 'HU'
    return None


def description_from_page(soup):
    content = soup.select_one('section.py-5 .col-lg-8')
    if not content:
        return None
    parts = []
    for block in content.find_all('div', recursive=False):
        text = clean_text(block)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event(content, event, url):
    soup = BeautifulSoup(content, 'html.parser')
    title = clean_text(soup.select_one('.concert-hero h1')) or clean_text(event.get('title'))
    raw_date = clean_text(event.get('date')).split('T', 1)[0]
    try:
        event_date = datetime.strptime(raw_date, '%Y-%m-%d').date().isoformat()
    except ValueError:
        return None

    raw_time = clean_text(event.get('time'))
    time_from = None
    if raw_time:
        try:
            time_from = datetime.strptime(raw_time, '%H:%M:%S').strftime('%H:%M')
        except ValueError:
            return None

    location = parse_location(soup, event, title)
    if not title or not location:
        return None
    venue, city, country_code = location
    if not all((venue, city, country_code)) or venue.casefold() == city.casefold():
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description_from_page(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = make_session()
    records = []
    for event in api_events(session):
        url = urljoin(SOURCE_URL, clean_text(event.get('slug')).lstrip('/'))
        try:
            response = session.get(url, timeout=60)
            response.raise_for_status()
            record = parse_event(response.content, event, url)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape MÁV Symphony Orchestra concert',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)
        else:
            log_message(
                'Skipped MÁV concert with incomplete date or location',
                event='crawler_item_skipped',
                level='warning',
                url=url,
            )
    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


class MavzenekarHuCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mavzenekar_hu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='HU',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    MavzenekarHuCrawler().run()


if __name__ == '__main__':
    main()
