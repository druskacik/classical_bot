import base64
import hashlib
import html
import random
import re
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.dunedin-consort.org.uk/'
SOURCE = 'Dunedin Consort'
API_URL = urljoin(SOURCE_URL, 'wp-json/tribe/events/v1/events')
START_DATE = '1900-01-01'
END_DATE = '2100-12-31'
PER_PAGE = 50

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/html;q=0.9',
    'Accept-Language': 'en-GB,en;q=0.9',
}

COUNTRY_CODES = {
    'austria': 'AT',
    'belgium': 'BE',
    'canada': 'CA',
    'czech republic': 'CZ',
    'denmark': 'DK',
    'france': 'FR',
    'germany': 'DE',
    'ireland': 'IE',
    'italy': 'IT',
    'netherlands': 'NL',
    'norway': 'NO',
    'poland': 'PL',
    'portugal': 'PT',
    'spain': 'ES',
    'sweden': 'SE',
    'switzerland': 'CH',
    'united kingdom': 'GB',
    'uk': 'GB',
    'united states': 'US',
    'united states of america': 'US',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _challenge_location(response):
    if response.status_code != 202:
        return None
    match = re.search(r'<meta[^>]+content=["\']0;([^"\']+)', response.text, re.I)
    return urljoin(response.url, html.unescape(match.group(1))) if match else None


def _solve_siteground_challenge(session, response):
    challenge_url = _challenge_location(response)
    if not challenge_url:
        return False

    page = session.get(challenge_url, timeout=45)
    page.raise_for_status()
    challenge_match = re.search(r'const sgchallenge="([^"]+)"', page.text)
    submit_match = re.search(r'const sgsubmit_url="([^"]+)"', page.text)
    if not challenge_match or not submit_match:
        return False

    challenge = challenge_match.group(1)
    complexity = int(challenge.split(':', 1)[0])
    seed = challenge.encode('utf-8')
    counter = random.randrange(5_000_000)
    started = time.monotonic()

    for hashes in range(5_000_001):
        counter_bytes = counter.to_bytes(max(1, (counter.bit_length() + 7) // 8), 'big')
        solution = seed + counter_bytes
        solution += b'\0' * (-len(solution) % 4)
        digest_prefix = int.from_bytes(hashlib.sha1(solution).digest()[:4], 'big')
        if digest_prefix >> (32 - complexity) == 0:
            break
        counter += 1
    else:
        raise RuntimeError('SiteGround challenge solution was not found')

    elapsed_ms = int((time.monotonic() - started) * 1000)
    submit_url = urljoin(page.url, submit_match.group(1))
    solved = session.get(
        submit_url,
        params={
            'sol': base64.b64encode(solution).decode('ascii'),
            's': f'{elapsed_ms}:{hashes}',
        },
        timeout=45,
    )
    return solved.status_code in (200, 202) and '_I_' in session.cookies


def api_get(session, params):
    response = session.get(API_URL, params=params, timeout=45)
    if response.status_code == 202:
        if not _solve_siteground_challenge(session, response):
            raise RuntimeError('Unable to pass the source website challenge')
        response = session.get(API_URL, params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def parse_event(event):
    title = clean_text(event.get('title'))
    url = event.get('url')
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    country = clean_text(venue_data.get('country')).casefold()
    country_code = COUNTRY_CODES.get(country) if country else 'GB'

    try:
        start = datetime.strptime(event.get('start_date', ''), '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None

    # A missing country is normally a legacy UK venue on this Scottish
    # ensemble's calendar. Unknown explicit countries are not guessed.
    if not all((title, url, venue, city, country_code)):
        return None

    time_from = None if event.get('all_day') else start.strftime('%H:%M')
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1
    total_pages = 1

    while page <= total_pages:
        payload = api_get(session, {
            'per_page': PER_PAGE,
            'page': page,
            'start_date': START_DATE,
            'end_date': END_DATE,
            'status': 'publish',
        })
        total_pages = int(payload.get('total_pages') or 1)
        for event in payload.get('events') or []:
            record = parse_event(event)
            if record:
                records.append(record)
        page += 1

    log_message(
        'Dunedin Consort API scrape completed',
        event='crawler_api_scrape_completed',
        record_count=len(records),
    )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )


class DunedinConsortOrgUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='dunedin_consort_org_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
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
    DunedinConsortOrgUkCrawler().run()


if __name__ == '__main__':
    main()
