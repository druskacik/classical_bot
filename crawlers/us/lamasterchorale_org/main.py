import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://lamasterchorale.org/'
SOURCE = 'Los Angeles Master Chorale'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\ufeff', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session


def get_soup(session, url, parser='html.parser'):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, parser)


def detail_urls_from_sitemap(soup):
    source_host = urlparse(SOURCE_URL).netloc
    urls = []
    for location in soup.find_all('loc'):
        url = clean_text(location)
        parsed = urlparse(url)
        if parsed.netloc == source_host and parsed.path.startswith('/show-details/'):
            urls.append(url)
    return list(dict.fromkeys(urls))


def event_json(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, ValueError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') in {'Event', 'MusicEvent'}:
                return candidate
    return {}


def parse_date(value):
    value = clean_text(value)
    for pattern in ('%b %d, %Y', '%B %d, %Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2})(?::([0-5]\d))?\s*([AP])M\b', clean_text(value), re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).upper() == 'P':
        hour += 12
    return f'{hour:02d}:{match.group(2) or "00"}'


def occurrences_from_table(soup):
    occurrences = []
    table = soup.select_one('#timeline table')
    for row in table.select('tbody tr') if table else []:
        cells = row.find_all('td')
        if len(cells) < 3:
            continue
        event_date = parse_date(cells[0])
        venue = clean_text(cells[2])
        if event_date and venue:
            occurrences.append((event_date, parse_time(cells[1]), venue))
    return occurrences


def occurrence_from_json(payload):
    start = clean_text(payload.get('startDate'))
    if not start:
        return []
    event_date = parse_date(start.split('T', 1)[0])
    location = payload.get('location') if isinstance(payload.get('location'), dict) else {}
    venue = clean_text(location.get('name'))
    if not event_date or not venue:
        return []
    return [(event_date, parse_time(start.split('T', 1)[1]) if 'T' in start else None, venue)]


def parse_detail_page(soup, url):
    payload = event_json(soup)
    title = clean_text(soup.select_one('.bg-heading h1')) or clean_text(payload.get('name'))
    location = payload.get('location') if isinstance(payload.get('location'), dict) else {}
    address = location.get('address') if isinstance(location.get('address'), dict) else {}
    city = clean_text(address.get('addressLocality'))
    country_code = clean_text(address.get('addressCountry')).upper() or 'US'
    about = soup.select_one('#about')
    description = clean_text(about) or clean_text(payload.get('description')) or None
    occurrences = occurrences_from_table(soup) or occurrence_from_json(payload)

    if not title or not city or len(country_code) != 2:
        return []

    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from, venue in occurrences
    ]


class LamasterchoraleOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lamasterchorale_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = make_session()
        try:
            detail_urls = detail_urls_from_sitemap(get_soup(session, SITEMAP_URL, 'xml'))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Los Angeles Master Chorale sitemap',
                event='crawler_listing_request_failed',
                level='error',
                url=SITEMAP_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        finally:
            session.close()

        records = []

        def fetch_detail(detail_url):
            detail_session = make_session()
            try:
                return parse_detail_page(get_soup(detail_session, detail_url), detail_url)
            finally:
                detail_session.close()

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fetch_detail, url): url for url in detail_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Los Angeles Master Chorale event detail',
                        event='crawler_detail_request_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        if not records:
            log_message(
                'Los Angeles Master Chorale scrape returned no concerts',
                event='crawler_empty_result',
                level='warning',
                url=SITEMAP_URL,
                record_count=0,
            )
        return records


def main():
    return LamasterchoraleOrgCrawler().run()


if __name__ == '__main__':
    main()
