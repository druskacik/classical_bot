import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://verbierfestival.com/'
PROGRAMME_URL = urljoin(SOURCE_URL, 'programme/')
SOURCE = 'Verbier Festival'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-CH,fr;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = html.unescape(str(value))
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def parse_start(value, fallback_date):
    if value and value.isdigit() and len(value) <= 10:
        start = datetime.fromtimestamp(int(value))
        return start.date().isoformat(), start.strftime('%H:%M')
    match = re.fullmatch(r'(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(?:\d{2})?', value or '')
    if match:
        return f'{match[1]}-{match[2]}-{match[3]}', f'{match[4]}:{match[5]}'
    try:
        return datetime.strptime(fallback_date, '%Y-%m-%d').date().isoformat(), None
    except (TypeError, ValueError):
        return None, None


def city_for_venue(venue):
    normalized = venue.casefold()
    if 'saint-maurice' in normalized or 'saint maurice' in normalized:
        return 'Saint-Maurice'
    if 'espace saint-marc' in normalized or 'le châble' in normalized or 'le chable' in normalized:
        return 'Le Châble'
    # The programme is the local festival calendar. These are its named
    # Verbier halls and outdoor sites; touring events are handled above.
    return 'Verbier'


def listing_records(content):
    soup = BeautifulSoup(content, 'html.parser')
    records = []
    seen = set()
    for link in soup.select('a[href*="/ical.php?"]'):
        query = parse_qs(urlparse(link.get('href', '')).query)
        url = clean_text((query.get('desc') or [''])[0])
        title = clean_text((query.get('eventTitle') or [''])[0])
        venue = clean_text((query.get('eventLocation') or [''])[0])
        event_date, time_from = parse_start(
            clean_text((query.get('startTime') or [''])[0]),
            clean_text((query.get('date') or [''])[0]),
        )
        if not url.startswith(SOURCE_URL + 'show/'):
            continue
        url = urljoin(SOURCE_URL, url)
        key = (url, event_date, time_from)
        if key in seen or not title or not event_date or not venue:
            continue
        seen.add(key)
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city_for_venue(venue),
            'country_code': 'CH',
            'description': None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def detail_description(content):
    soup = BeautifulSoup(content, 'html.parser')
    parts = []
    for selector, heading in (
        ('.hero-header__description', None),
        ('.program-content', 'Programme'),
        ('.distribution-content', 'Interprètes'),
    ):
        text = clean_text(soup.select_one(selector))
        if text:
            parts.append(f'{heading}\n{text}' if heading else text)
    return '\n\n'.join(dict.fromkeys(parts)) or None


def get_concerts():
    session = make_session()
    records = listing_records(get_response(session, PROGRAMME_URL).text)
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_response, session, record['url']): record for record in records}
        for future in as_completed(futures):
            record = futures[future]
            try:
                record['description'] = detail_description(future.result().text)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Verbier Festival event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class VerbierFestivalComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='verbierfestival_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
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
    VerbierFestivalComCrawler().run()


if __name__ == '__main__':
    main()
