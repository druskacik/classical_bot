import html
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.festivalberlioz.com/'
SOURCE = 'Festival Berlioz'
API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/programmation')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.5',
}
MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def folded(value):
    text = unicodedata.normalize('NFKD', clean_text(value).casefold())
    return ''.join(char for char in text if not unicodedata.combining(char))


def parse_datetime(value, year):
    match = re.search(
        r'\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)(?:\s+(20\d{2}))?'
        r'(?:\s+(?:a|à)\s+([01]?\d|2[0-3])\s*h\s*([0-5]\d)?)?',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None, None
    month = MONTHS.get(folded(match.group(2)))
    if not month:
        return None, None
    try:
        event_date = date(int(match.group(3) or year), month, int(match.group(1))).isoformat()
    except ValueError:
        return None, None
    event_time = None
    if match.group(4) is not None:
        event_time = f'{int(match.group(4)):02d}:{int(match.group(5) or 0):02d}'
    return event_date, event_time


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )))
    return session


def fetch_all_posts(session):
    posts = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page, 'orderby': 'id', 'order': 'asc'},
            timeout=45,
        )
        response.raise_for_status()
        posts.extend(response.json())
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return posts


def parse_detail(session, post):
    url = post.get('link', '').strip()
    if not url:
        return None
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    title = clean_text((post.get('title') or {}).get('rendered'))
    year_match = re.match(r'(20\d{2})', post.get('date', ''))
    year = int(year_match.group(1)) if year_match else date.today().year

    event_date = event_time = None
    for shortcode in soup.select('.elementor-shortcode'):
        candidate_date, candidate_time = parse_datetime(clean_text(shortcode), year)
        if candidate_date:
            event_date, event_time = candidate_date, candidate_time
            break

    venue_link = soup.select_one('.elementor-shortcode a[href*="/lieux/"]')
    venue = clean_text(venue_link)
    venue_url = urljoin(SOURCE_URL, venue_link.get('href', '')) if venue_link else None

    description = None
    if venue_link:
        artistic_section = venue_link.find_parent(
            'div', class_=lambda value: value and 'e-con' in value.split()
        )
        description = clean_text(artistic_section) or None
    if not description:
        rendered = (post.get('content') or {}).get('rendered', '')
        description = clean_text(BeautifulSoup(rendered, 'html.parser')) or None

    if not all((title, event_date, venue, venue_url)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'venue_url': venue_url,
        'description': description,
    }


def fetch_venue_city(session, venue_url, venue):
    response = session.get(venue_url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    for heading in soup.select('h1, h2, h3'):
        if clean_text(heading) != venue:
            continue
        container = heading.find_parent(
            'div', class_=lambda value: value and 'e-con' in value.split()
        )
        lines = clean_text(container).splitlines() if container else []
        for index, line in enumerate(lines):
            if line == venue and index + 1 < len(lines):
                city = lines[index + 1]
                if city and city != venue:
                    return city
    return None


class FestivalBerliozComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festivalberlioz_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = make_session()
        posts = fetch_all_posts(session)
        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(parse_detail, session, post): post for post in posts}
            for future in as_completed(futures):
                post = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Festival Berlioz event detail request failed',
                        event='crawler_event_detail_failed',
                        level='warning',
                        url=post.get('link'),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        venues = {(record['venue_url'], record['venue']) for record in records}
        cities = {}
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(fetch_venue_city, session, venue_url, venue): venue_url
                for venue_url, venue in venues
            }
            for future in as_completed(futures):
                venue_url = futures[future]
                try:
                    cities[venue_url] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Festival Berlioz venue request failed',
                        event='crawler_venue_failed',
                        level='warning',
                        url=venue_url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        valid_records = []
        for record in records:
            city = cities.get(record.pop('venue_url'))
            if not city:
                continue
            record['city'] = city
            valid_records.append(record)

        valid_records.sort(key=lambda row: (
            row['date'], row['time_from'] or '', row['title'], row['venue'], row['url']
        ))
        log_message(
            'Festival Berlioz programme scraped',
            event='crawler_scrape_completed',
            level='info',
            url=API_URL,
            record_count=len(valid_records),
        )
        return valid_records


def main():
    return FestivalBerliozComCrawler().run()


if __name__ == '__main__':
    main()
