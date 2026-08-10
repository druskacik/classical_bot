import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sfilarmonicavalencia.com/'
ARCHIVE_URL = urljoin(SOURCE_URL, 'temporadas-anteriores-sfv')
SOURCE = 'Sociedad Filarmónica de Valencia'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9',
}

DATE_RE = re.compile(r'\b\d{1,2}/\d{1,2}/\d{4}\b')
TIME_VENUE_PATTERNS = (
    re.compile(
        r'(?P<time>\d{1,2}[.:]\d{2})\s*h?\s*,\s*(?P<venue>[^\n]+)',
        re.IGNORECASE,
    ),
    re.compile(
        r'la hora del concierto es a las\s+(?P<time>\d{1,2}[.:]\d{2})\s+'
        r'en el\s+(?P<venue>[^\n]+)',
        re.IGNORECASE,
    ),
)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def season_urls(session):
    html = str(get_soup(session, ARCHIVE_URL))
    urls = set(re.findall(
        r'https://www\.sfilarmonicavalencia\.com/temporada-[^"\\< ]+',
        html,
    ))
    return sorted(url.rstrip('/') for url in urls)


def detail_urls(session):
    urls = set()
    for season_url in season_urls(session):
        soup = get_soup(session, season_url)
        for link in soup.select('a[href*="/post/"]'):
            url = urljoin(SOURCE_URL, link.get('href', '')).split('?', 1)[0]
            if '/post/' in url:
                urls.add(url)
    return sorted(urls)


def parse_detail(soup, url):
    title_node = soup.select_one('[data-hook="post-title"]')
    body_node = soup.select_one('[data-hook="post-description"]')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    description = clean_text(body_node.get_text('\n', strip=True) if body_node else '')
    date_match = DATE_RE.search(description)
    time_venue_match = next(
        (match for pattern in TIME_VENUE_PATTERNS if (match := pattern.search(description))),
        None,
    )
    if not title or not date_match or not time_venue_match:
        return None

    try:
        event_date = datetime.strptime(date_match.group(), '%d/%m/%Y').date().isoformat()
        raw_time = time_venue_match.group('time').replace('.', ':')
        time_from = datetime.strptime(raw_time, '%H:%M').strftime('%H:%M')
    except ValueError:
        return None

    venue = clean_text(time_venue_match.group('venue'))
    venue = re.split(r'\s+PROGRAMA\b', venue, maxsplit=1, flags=re.IGNORECASE)[0].strip(' .,')
    # The Almudín posts append a street address in parentheses; addresses are
    # deliberately not stored as part of the venue name.
    venue = re.sub(r'\s*\(plaza\s+San\s+Luis\s+Bertrán[^)]*\)\s*$', '', venue, flags=re.IGNORECASE)
    if not venue:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': 'Valencia',
        'country_code': 'ES',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_detail(session, url):
    return parse_detail(get_soup(session, url), url)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = detail_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(scrape_detail, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
                if record:
                    records.append(record)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'], record['title']),
    )


class SfilarmonicavalenciaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sfilarmonicavalencia_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    SfilarmonicavalenciaComCrawler().run()


if __name__ == '__main__':
    main()
