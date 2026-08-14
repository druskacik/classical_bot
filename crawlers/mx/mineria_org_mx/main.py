import base64
import hashlib
import html
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://mineria.org.mx/'
SOURCE = 'Sinfónica de Minería'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/conciertos-sinfonica'
CATEGORY_IDS = '27,28,29'  # Sinfónica, Pops, and Agrupaciones

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-MX,es;q=0.9,en;q=0.6',
}

MONTHS = {
    'enero': 1,
    'febrero': 2,
    'marzo': 3,
    'abril': 4,
    'mayo': 5,
    'junio': 6,
    'julio': 7,
    'agosto': 8,
    'septiembre': 9,
    'octubre': 10,
    'noviembre': 11,
    'diciembre': 12,
}

# The REST taxonomy supplies venue IDs but no addresses. These are the places
# used by published concert records, including the orchestra's US tour.
LOCATIONS = {
    34: ('Sala Nezahualcóyotl', 'Ciudad de México', 'MX'),
    54: ('Pepsi Center WTC', 'Ciudad de México', 'MX'),
    70: ('Teatro de la Ciudad Esperanza Iris', 'Ciudad de México', 'MX'),
    71: ('Gerald R. Ford Amphitheater', 'Vail', 'US'),
    237: ('Auditorio Nacional', 'Ciudad de México', 'MX'),
    254: ('Arena Ciudad de México', 'Ciudad de México', 'MX'),
    260: (
        'Teatro Ángel y Tere Losada, Centro Cultural Mexiquense Anáhuac',
        'Huixquilucan',
        'MX',
    ),
    271: ('Nottingham Park', 'Avon', 'US'),
    272: ('Centro Cultural Ollin Yoliztli', 'Ciudad de México', 'MX'),
    275: ('Plaza de Toros Oriente', 'San Miguel de Allende', 'MX'),
    276: ('Community Church of Vero Beach', 'Vero Beach', 'US'),
    277: ('Kravis Center', 'West Palm Beach', 'US'),
    280: ('Orchestra Hall', 'Chicago', 'US'),
    282: ('Helzberg Hall, Kauffman Center', 'Kansas City', 'US'),
    284: ('Hill Auditorium', 'Ann Arbor', 'US'),
    286: ('Cathedral City Community Amphitheater', 'Cathedral City', 'US'),
    287: ('McCallum Theatre', 'Palm Desert', 'US'),
    288: ('Segerstrom Concert Hall', 'Costa Mesa', 'US'),
    289: ('Vilar Performing Arts Center', 'Beaver Creek', 'US'),
    300: ('Zócalo de la Ciudad de México', 'Ciudad de México', 'MX'),
    301: ('Sala Principal del Palacio de Bellas Artes', 'Ciudad de México', 'MX'),
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = html.unescape(str(value))
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _counter_bytes(counter):
    return counter.to_bytes(max(1, (counter.bit_length() + 7) // 8), 'big')


def pass_siteground_challenge(session, response):
    """Complete SiteGround's public SHA-1 proof-of-work browser challenge."""
    refresh = re.search(r'content=["\']0;([^"\'<]+)', response.text)
    if response.status_code != 202 or not refresh:
        return response

    challenge_response = session.get(urljoin(response.url, refresh.group(1)), timeout=45)
    challenge_response.raise_for_status()
    challenge_match = re.search(r'const sgchallenge="([^"]+)"', challenge_response.text)
    submit_match = re.search(r'const sgsubmit_url="([^"]+)"', challenge_response.text)
    if not challenge_match or not submit_match:
        raise ValueError('SiteGround challenge payload was not recognized')

    challenge = challenge_match.group(1)
    challenge_bytes = challenge.encode('utf-8')
    complexity = int(challenge.split(':', 1)[0])
    threshold = 1 << (32 - complexity)
    started = time.monotonic()
    solution = None
    counter = 1
    while counter <= 40_000_000:
        payload = challenge_bytes + _counter_bytes(counter)
        if int.from_bytes(hashlib.sha1(payload).digest()[:4], 'big') < threshold:
            solution = base64.b64encode(payload).decode('ascii')
            break
        counter += 1
    if solution is None:
        raise RuntimeError('SiteGround proof-of-work limit exceeded')

    elapsed_ms = max(1, int((time.monotonic() - started) * 1000))
    session.get(
        urljoin(challenge_response.url, submit_match.group(1)),
        params={'sol': solution, 's': f'{elapsed_ms}:{counter}'},
        timeout=45,
    ).raise_for_status()
    return session.get(response.url, timeout=45)


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    if response.status_code == 202:
        response = pass_siteground_challenge(session, response)
    response.raise_for_status()
    return response


def get_events(session):
    params = {
        'categorias-de-conciertos': CATEGORY_IDS,
        'per_page': 100,
        'page': 1,
        'orderby': 'date',
        'order': 'desc',
    }
    events = []
    while True:
        response = get_response(session, API_URL, params=params)
        page = response.json()
        events.extend(page)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if params['page'] >= total_pages:
            return events
        params['page'] += 1


def parse_event_date(fields, published):
    if len(fields) < 3:
        return None
    day_match = re.fullmatch(r'\d{1,2}', fields[1])
    month = MONTHS.get(fields[2].lower())
    if not day_match or not month:
        return None

    try:
        published_date = date.fromisoformat(published[:10])
        # Concert posts are published shortly before their occurrence. A
        # smaller event month on a late-year post denotes the following year.
        year = published_date.year + (month < published_date.month)
        return date(year, month, int(day_match.group())).isoformat()
    except (TypeError, ValueError):
        return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2}):(\d{2})\s*(am|pm)?\b', value, re.I)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    marker = (match.group(3) or '').lower()
    if minute > 59 or hour > (12 if marker else 23) or hour == 0 and marker:
        return None
    if marker == 'pm' and hour != 12:
        hour += 12
    elif marker == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def parse_detail(event, response):
    soup = BeautifulSoup(response.text, 'html.parser')
    fields = [clean_text(node) for node in soup.select('.jet-listing-dynamic-field__content')]
    event_date = parse_event_date(fields, event.get('date', ''))
    event_time = parse_time(fields[3]) if len(fields) > 3 else None
    title = clean_text((event.get('title') or {}).get('rendered'))
    url = clean_text(event.get('link'))

    location = None
    for venue_id in event.get('recintos') or []:
        if venue_id in LOCATIONS:
            location = LOCATIONS[venue_id]
            break
    if not title or not event_date or not url or not location:
        return None

    candidates = [value for value in fields[8:] if len(value) >= 80]
    description = max(candidates, key=len) if candidates else None
    venue, city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class MineriaOrgMxCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mineria_org_mx',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='MX',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            events = get_events(session)
        except (requests.RequestException, ValueError, RuntimeError) as error:
            log_message(
                'Failed to fetch Sinfónica de Minería event feed',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(get_response, session, event['link']): event
                for event in events
                if event.get('link')
            }
            for future in as_completed(futures):
                event = futures[future]
                try:
                    record = parse_detail(event, future.result())
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to fetch Sinfónica de Minería concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=event.get('link'),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    MineriaOrgMxCrawler().run()


if __name__ == '__main__':
    main()
