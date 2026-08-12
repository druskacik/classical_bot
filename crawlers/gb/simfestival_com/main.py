import base64
import hashlib
import html
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://simfestival.com/'
SOURCE = 'Stamford International Music Festival'
API_URL = 'https://public-api.wordpress.com/wp/v2/sites/simfestival.com/concert'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

CITY_BY_VENUE = {
    'All Saints’ Church': 'Stamford',
    "All Saints' Church": 'Stamford',
    'Browne’s Hospital': 'Stamford',
    "Browne's Hospital": 'Stamford',
    'Barn Hill Methodist Church': 'Stamford',
    'Elliot Oswald Hall': 'Stamford',
    'St. Martin’s Church': 'Stamford',
    "St. Martin's Church": 'Stamford',
    'St. Mary’s Church': 'Stamford',
    "St. Mary's Church": 'Stamford',
    'St Martin’s Church': 'Stamford',
    "St Martin's Church": 'Stamford',
    'Stamford Arts Centre': 'Stamford',
    'Stamford Methodist Church': 'Stamford',
    'St Peter and St Paul’s Church': 'Uppingham',
    "St Peter and St Paul's Church": 'Uppingham',
    'Uppingham Parish Church': 'Uppingham',
    'Uppingham School Memorial Hall': 'Uppingham',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def api_concerts():
    response = requests.get(API_URL, params={'per_page': 100}, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response.json()


def _counter_bytes(counter):
    return counter.to_bytes(max(1, (counter.bit_length() + 7) // 8), 'big')


def pass_robot_challenge(session):
    response = session.get(f'{SOURCE_URL}concerts/', timeout=45)
    if response.status_code == 200 and len(response.content) > 1000:
        return

    redirect_match = re.search(r'<meta[^>]+content="0;([^\"]+)"', response.text)
    if not redirect_match:
        response.raise_for_status()
        raise RuntimeError('SiteGround challenge redirect was not found')

    challenge_url = requests.compat.urljoin(SOURCE_URL, html.unescape(redirect_match.group(1)))
    challenge_response = session.get(challenge_url, timeout=45)
    challenge_response.raise_for_status()
    challenge_match = re.search(r'const sgchallenge="([^"]+)"', challenge_response.text)
    submit_match = re.search(r'const sgsubmit_url="([^"]+)"', challenge_response.text)
    if not challenge_match or not submit_match:
        raise RuntimeError('SiteGround proof-of-work parameters were not found')

    challenge = challenge_match.group(1)
    complexity = int(challenge.split(':', 1)[0])
    challenge_bytes = challenge.encode()
    started = time.monotonic()
    solution = None
    hashes = 0
    for counter in range(1, 40_000_001):
        candidate = challenge_bytes + _counter_bytes(counter)
        digest_word = int.from_bytes(hashlib.sha1(candidate).digest()[:4], 'big')
        if digest_word >> (32 - complexity) == 0:
            solution = base64.b64encode(candidate).decode()
            hashes = counter
            break
    if solution is None:
        raise RuntimeError('SiteGround proof-of-work challenge could not be solved')

    elapsed_ms = int((time.monotonic() - started) * 1000)
    submit_url = requests.compat.urljoin(SOURCE_URL, html.unescape(submit_match.group(1)))
    result = session.get(
        submit_url,
        params={'sol': solution, 's': f'{elapsed_ms}:{hashes}'},
        timeout=45,
    )
    result.raise_for_status()


def parse_date(value):
    value = re.sub(r'(\d)(?:st|nd|rd|th)\b', r'\1', value, flags=re.IGNORECASE)
    value = value.replace(',', '')
    for pattern in ('%A %d %B %Y', '%a %d %B %Y'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def parse_time(value):
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value.upper().replace('.', ''), pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def field_value(header, icon_class):
    icon = header.select_one(f'.{icon_class}')
    return clean_text(icon.parent).replace(clean_text(icon), '', 1).strip() if icon else ''


def parse_concert(content, item):
    soup = BeautifulSoup(content, 'html.parser')
    header = soup.select_one('header.concert .event-details')
    if not header:
        return None

    title = clean_text(header.select_one('h1')) or clean_text(
        BeautifulSoup(item['title']['rendered'], 'html.parser')
    )
    event_date = parse_date(field_value(header, 'date-icon'))
    venue = field_value(header, 'location-icon')
    city = CITY_BY_VENUE.get(venue)
    if not title or not event_date or not venue or not city:
        return None

    programme = None
    for heading in soup.select('main h2'):
        if clean_text(heading).lower() != 'programme':
            continue
        programme_node = heading.find_next('ul')
        if programme_node:
            programme = clean_text(programme_node)
        break

    body = clean_text(BeautifulSoup(item['content']['rendered'], 'html.parser'))
    description_parts = []
    if programme:
        description_parts.append(f'Programme\n{programme}')
    if body:
        description_parts.append(body)

    return {
        'title': title,
        'date': event_date,
        'url': item['link'],
        'time_from': parse_time(field_value(header, 'time-icon')),
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    items = api_concerts()
    session = requests.Session()
    session.headers.update(HEADERS)
    pass_robot_challenge(session)
    records = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(session.get, item['link'], timeout=45): item for item in items
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                response = future.result()
                response.raise_for_status()
                record = parse_concert(response.content, item)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped SIMFestival concert with incomplete location or date',
                        event='crawler_item_skipped',
                        level='warning',
                        url=item['link'],
                    )
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape SIMFestival concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=item['link'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class SimfestivalComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='simfestival_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
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
        return get_concerts()


def main():
    SimfestivalComCrawler().run()


if __name__ == '__main__':
    main()
