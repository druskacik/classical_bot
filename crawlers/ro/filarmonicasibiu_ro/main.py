import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.filarmonicasibiu.ro/'
SOURCE = 'Filarmonica de Stat Sibiu'
LIST_ENDPOINT = urljoin(SOURCE_URL, 'page_list')
LISTS = (
    ('elem_0291fbde78e7', 17846),  # current calendar
    ('elem_b81a911f000e', 43142),  # archive
)
DEFAULT_CITY = 'Sibiu'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ro-RO,ro;q=0.9,en;q=0.6',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def list_event_urls(session, element_id, page_id):
    page = 1
    while True:
        response = session.get(
            LIST_ENDPOINT,
            params={
                'element_id': element_id,
                'locale': 'ro',
                'page': page,
                'page_id': page_id,
            },
            timeout=60,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        container = soup.select_one('[data-infinite-scroll-next-url-value]')
        for link in soup.select('a[data-turbo-frame="_top"][href]'):
            yield urljoin(SOURCE_URL, link['href'])

        next_url = container.get('data-infinite-scroll-next-url-value') if container else None
        if not next_url:
            break
        page += 1


def extract_venue(description):
    # Event pages normally open with "date, ora HH:MM, venue". Keep only the
    # first line so ticket and programme prose cannot leak into the venue.
    first_lines = '\n'.join(description.splitlines()[:4])
    match = re.search(
        r'\bora\s+\d{1,2}(?:[.:]\d{2})?(?:\s*(?:&|și|si)\s*ora\s*\d{1,2}(?:[.:]\d{2})?)?'
        r'\s*[,|–-]\s*([^\n|]+)',
        first_lines,
        flags=re.IGNORECASE,
    )
    if not match:
        return ''
    venue = clean_text(match.group(1)).strip(' .,:;–-')
    venue = re.split(r'\s{2,}|\b(?:Bilete|Program|Soliști|Solisti|Dirijor)\b', venue)[0].strip()
    if len(venue) < 3 or len(venue) > 140:
        return ''
    return venue


def parse_event(session, url):
    # Detail pages are lazy Turbo frames; this header returns the actual frame
    # instead of the full-page loading skeleton.
    response = session.get(url, headers={'Turbo-Frame': 'page_content'}, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    title_node = soup.select_one('h1')
    time_node = soup.select_one('time[datetime*="T"]')
    description_node = soup.select_one('.trix-content')
    if not title_node or not time_node:
        return None

    title = clean_text(title_node.get_text(' ', strip=True))
    raw_datetime = clean_text(time_node.get('datetime'))
    try:
        start = datetime.fromisoformat(raw_datetime)
    except (TypeError, ValueError):
        return None

    description = clean_text(description_node.get_text('\n', strip=True)) if description_node else ''
    venue = extract_venue(description)
    if not title or not venue:
        return None

    # The calendar is Sibiu-based. Pages explicitly describing tours need a
    # per-event location; do not silently apply the home-city default to them.
    location_evidence = f'{venue}\n{description[:500]}'.lower()
    touring = any(word in location_evidence for word in ('turneu', 'în deplasare', 'in deplasare'))
    if touring and 'sibiu' not in location_evidence:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': DEFAULT_CITY,
        'description': description or None,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = []
    seen_urls = set()
    records = []
    try:
        for element_id, page_id in LISTS:
            for url in list_event_urls(session, element_id, page_id):
                if url not in seen_urls:
                    seen_urls.add(url)
                    urls.append(url)

        for url in urls:
            try:
                record = parse_event(session, url)
            except requests.RequestException as error:
                log_message(
                    'Failed to retrieve event detail',
                    event='crawler_detail_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    except requests.RequestException as error:
        log_message(
            'Failed to retrieve event feed',
            event='crawler_feed_failed',
            level='error',
            url=LIST_ENDPOINT,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'], record['title'], record['url']),
    )


class FilarmonicaSibiuRoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filarmonicasibiu_ro',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='RO',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    FilarmonicaSibiuRoCrawler().run()


if __name__ == '__main__':
    main()
