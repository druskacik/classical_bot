import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cso.org/'
SOURCE = 'Chicago Symphony Orchestra'
CALENDAR_URL = f'{SOURCE_URL}umbraco/surface/events/calendar'
TOKEN_URL = f'{SOURCE_URL}antiforgery/token'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET', 'POST'),
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session


def fetch_calendar(session):
    token_response = session.get(TOKEN_URL, timeout=45)
    token_response.raise_for_status()
    token = token_response.json()['token']
    headers = {
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json;charset=UTF-8',
        'RequestVerificationToken': token,
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': f'{SOURCE_URL}concerts-tickets/whats-on/',
    }
    start = datetime.now(timezone.utc).date().isoformat()
    payload = {
        'from': f'{start}T00:00:00.000Z',
        'paid': '',
        'group': True,
        'concerttypes': [],
        'genres': [],
        'venues': [],
        'platforms': [],
        'seasons': [],
        'page': 0,
    }

    productions = []
    total = None
    page = 0
    while total is None or len(productions) < total:
        payload['page'] = page
        response = session.post(CALENDAR_URL, json=payload, headers=headers, timeout=45)
        response.raise_for_status()
        data = response.json()
        batch = data.get('productions') or []
        total = int(data.get('eventsCount') or 0)
        productions.extend(batch)
        if not batch:
            break
        page += 1

    return list({item.get('url') or item.get('contentUrl'): item for item in productions}.values())


def json_ld_event(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        raw = html.unescape(node.string or node.get_text())
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') in {'Event', 'MusicEvent'}:
                return candidate
    return None


def parse_occurrence(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def parse_detail(production, session=None):
    url = urljoin(SOURCE_URL, production.get('url') or production.get('contentUrl') or '')
    if not url:
        return []
    session = session or make_session()
    response = session.get(url, timeout=45)
    response.raise_for_status()
    payload = json_ld_event(BeautifulSoup(response.text, 'html.parser'))
    if not payload:
        return []

    title_value = production.get('title') or {}
    title = clean_text(title_value.get('value') if isinstance(title_value, dict) else title_value)
    title = title or clean_text(payload.get('name'))
    description_parts = [
        clean_text(production.get('productionSeasonDescriptionShort')),
        clean_text(production.get('productionSeasonDescriptionLong')),
        clean_text(payload.get('description')),
    ]
    description = '\n\n'.join(dict.fromkeys(part for part in description_parts if part)) or None

    occurrences = payload.get('subEvent') or [payload]
    if isinstance(occurrences, dict):
        occurrences = [occurrences]
    records = []
    for occurrence in occurrences:
        if not isinstance(occurrence, dict):
            continue
        parsed = parse_occurrence(occurrence.get('startDate'))
        location = occurrence.get('location') or payload.get('location') or {}
        address = location.get('address') or {} if isinstance(location, dict) else {}
        venue = clean_text(location.get('name')) if isinstance(location, dict) else ''
        city = clean_text(address.get('addressLocality')) if isinstance(address, dict) else ''
        country = clean_text(address.get('addressCountry')) if isinstance(address, dict) else ''
        venue_tags = production.get('venues') or []
        if 'Ravinia Festival' in venue_tags:
            venue, city = 'Ravinia Festival', 'Highland Park'
        elif 'Wheaton College' in venue_tags:
            venue, city = 'Wheaton College', 'Wheaton'
        elif 'Neighborhood venue' in venue_tags:
            venue_match = re.search(r'\s+at\s+(.+)$', title, re.IGNORECASE)
            venue = clean_text(venue_match.group(1)) if venue_match else ''
            venue = re.sub(r'^the\s+', '', venue, flags=re.IGNORECASE)
            city = 'Chicago'
        if not parsed or not title or not venue or not city:
            continue
        records.append({
            'title': title,
            'date': parsed[0],
            'url': url,
            'time_from': parsed[1],
            'venue': venue,
            'city': city,
            'country_code': country.upper() if re.fullmatch(r'[A-Za-z]{2}', country) else 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class CsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cso_org',
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
        session = make_session()
        try:
            productions = fetch_calendar(session)
        except (requests.RequestException, KeyError, ValueError) as error:
            log_message(
                'Failed to fetch CSO calendar',
                event='crawler_listing_request_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        finally:
            session.close()

        records = []

        def fetch(production):
            detail_session = make_session()
            try:
                return parse_detail(production, detail_session)
            finally:
                detail_session.close()

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch, item): item for item in productions}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch CSO event detail',
                        event='crawler_detail_request_failed',
                        level='warning',
                        url=item.get('url') or item.get('contentUrl'),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        if not records:
            log_message(
                'No CSO event occurrences found',
                event='crawler_empty_listing',
                level='warning',
                url=CALENDAR_URL,
                record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


def main():
    CsoOrgCrawler().run()


if __name__ == '__main__':
    main()
