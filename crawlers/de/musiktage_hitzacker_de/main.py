import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.musiktage-hitzacker.de/'
SOURCE = 'Sommerliche Musiktage Hitzacker'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
HEADERS = {
    'User-Agent': 'classical-concert-crawler/1.0',
    'Accept-Language': 'de-DE,de;q=0.9',
}
TIME_RE = re.compile(
    r'^\s*(?P<start>[0-2]?\d)[.:](?P<minute>[0-5]\d)'
    r'(?:\s*[–-]\s*[0-2]?\d[.:][0-5]\d)?\s+(?P<venue>.+)$'
)
DAY_RE = re.compile(r'\b(?P<day>\d{1,2})\.(?P<month>\d{1,2})\b')
YEAR_RE = re.compile(r'/programm-(?P<year>20\d{2})/')
SEPARATOR_RE = re.compile(r'^_+$')
NON_CONCERT_RE = re.compile(
    r'\b(?:mitgliederempfang|blitzlicht|forum nachhaltigkeit|kuchenpause)\b',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
    )))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def programme_urls(session):
    soup = get_soup(session, SITEMAP_URL)
    urls = []
    for location in soup.find_all('loc'):
        url = clean_text(location)
        if YEAR_RE.search(url) and re.search(r'\d{1,2}-\d{1,2}\.html$', url):
            urls.append(url)
    return sorted(set(urls))


def parse_event_date(soup, url):
    heading = clean_text(soup.select_one('main h1'))
    day_match = DAY_RE.search(heading)
    year_match = YEAR_RE.search(url)
    if not day_match or not year_match:
        return None
    try:
        return date(
            int(year_match.group('year')),
            int(day_match.group('month')),
            int(day_match.group('day')),
        ).isoformat()
    except ValueError:
        return None


def parse_header(paragraph):
    # The first visual line consistently contains time and venue. Subsequent
    # lines are format labels (for example "Acht nach Acht") or a title.
    first_line = clean_text(next(iter(paragraph.stripped_strings), ''))
    match = TIME_RE.match(first_line)
    if not match:
        return None
    hour = int(match.group('start'))
    if hour > 23:
        return None
    venue = re.split(
        r',\s*(?:Eintritt|Zutritt|Teilnahme)\s+frei\b',
        match.group('venue'), maxsplit=1, flags=re.IGNORECASE,
    )[0].strip(' ,')
    if not venue:
        return None
    return f'{hour:02d}:{match.group("minute")}', venue


def title_from_block(header, body):
    # Strong text is used for actual event titles. If absent, the first body
    # paragraph is the site's best available title (occasionally artist names).
    candidates = [clean_text(node) for node in header.select('strong')]
    for paragraph in body:
        candidates.extend(clean_text(node) for node in paragraph.select('strong'))
        if clean_text(paragraph):
            candidates.append(clean_text(paragraph))
    for candidate in candidates:
        if candidate and candidate.casefold() != 'tickets online':
            return candidate
    return None


def parse_programme(soup, url):
    event_date = parse_event_date(soup, url)
    main = soup.select_one('main')
    if not event_date or not main:
        return []

    paragraphs = main.select('p')
    starts = [index for index, paragraph in enumerate(paragraphs) if parse_header(paragraph)]
    records = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(paragraphs)
        header = paragraphs[start]
        body = [
            paragraph for paragraph in paragraphs[start + 1:end]
            if clean_text(paragraph)
            and not SEPARATOR_RE.fullmatch(clean_text(paragraph))
            and clean_text(paragraph).casefold() != 'tickets online'
        ]
        time_from, venue = parse_header(header)
        title = title_from_block(header, body)
        if not title or NON_CONCERT_RE.search(title):
            continue
        description_parts = []
        for paragraph in [header, *body]:
            text = clean_text(paragraph)
            if text and text not in description_parts:
                description_parts.append(text)
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': 'Hitzacker (Elbe)',
            'country_code': 'DE',
            'description': '\n\n'.join(description_parts) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class MusiktageHitzackerDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musiktage_hitzacker_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = make_session()
        records = []
        for url in programme_urls(session):
            try:
                records.extend(parse_programme(get_soup(session, url), url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Sommerliche Musiktage programme page',
                    event='crawler_page_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
        unique = {
            (item['url'], item['date'], item['time_from'], item['venue']): item
            for item in records
        }
        return sorted(unique.values(), key=lambda item: (
            item['date'], item['time_from'] or '', item['venue'], item['title'],
        ))


def main():
    MusiktageHitzackerDeCrawler().run()


if __name__ == '__main__':
    main()
