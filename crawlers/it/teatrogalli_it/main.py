import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://teatrogalli.it/'
SOURCE = 'Teatro Amintore Galli'

CURRENT_FEEDS = [
    'tipo-spettacolo/prosa',
    'tipo-spettacolo/musica',
    'tipo-spettacolo/opera',
    'tipo-spettacolo/danza',
    'tipo-spettacolo/altre-iniziative',
]
ARCHIVE_INDEX = urljoin(SOURCE_URL, 'pagina/archivio-stagioni')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}
MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}
DATE_RE = re.compile(
    r'(?:lunedi|lunedì|martedi|martedì|mercoledi|mercoledì|giovedi|giovedì|'
    r'venerdi|venerdì|sabato|domenica)?\s*'
    r'(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\s+(\d{4})'
    r'(?:\s*[-,]?\s*(?:ore\s*)?(\d{1,2})(?:[,.\s:]+(\d{2}))?)?',
    re.I,
)
CATEGORY_NAMES = {
    'prosa', 'musica', 'opera', 'danza', 'altre iniziative',
    'concerti sinfonici', 'musica da camera', 'percuotere la mente',
    'parole per la musica', 'concerti d’organo', "concerti d'organo",
}
VENUE_RE = re.compile(
    r'\b(?:Teatro|Sala|Corte|Arena|Chiesa|Auditorium|Piazza|Museo|Complesso|'
    r'Castel|Sagrato|Chiostro|Palazzo|Giardino|Anfiteatro)\b.*', re.I,
)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def event_links(soup):
    return {
        urljoin(SOURCE_URL, link['href'])
        for link in soup.select('a[href*="/eventonew/"]')
    }


def archive_pages(soup):
    return {
        urljoin(SOURCE_URL, link['href'])
        for link in soup.select('main a[href*="/archivio-stagioni/"]')
    }


def parse_dates(text):
    occurrences = []
    for match in DATE_RE.finditer(text):
        try:
            hour = int(match.group(4)) if match.group(4) else None
            minute = int(match.group(5)) if match.group(5) else 0
            if hour is not None and not (0 <= hour <= 23 and 0 <= minute <= 59):
                hour = None
            occurrences.append((
                date(
                    int(match.group(3)),
                    MONTHS[match.group(2).casefold()],
                    int(match.group(1)),
                ).isoformat(),
                f'{hour:02d}:{minute:02d}' if hour is not None else None,
            ))
        except (KeyError, ValueError):
            continue
    return occurrences


def parse_venue(date_node, text):
    # Editors consistently place the shared venue after the occurrence lines.
    for link in date_node.select('a[href*="tipo-spettacolo"]'):
        link.decompose()
    remainder = DATE_RE.sub('\n', clean_text(date_node))
    candidates = []
    for line in remainder.splitlines():
        line = re.sub(r'^[\s,;|–—-]+|[\s,;|–—-]+$', '', line).strip()
        if not line or line.casefold() in CATEGORY_NAMES or len(line) > 100:
            continue
        if re.search(r'bigliett|ingresso|durata|spettacol', line, re.I):
            continue
        venue_match = VENUE_RE.search(line)
        if venue_match:
            candidates.append(re.split(r'\s+[–—-]\s+', venue_match.group(0), maxsplit=1)[0].strip())
    if not candidates:
        return None
    venue = candidates[0]
    # An explicit trailing city wins; otherwise this municipal programme is in Rimini.
    city_match = re.search(r'\b(?:a|di)\s+([A-ZÀ-ÖØ-Ý][\wÀ-ÿ .\'-]+)$', venue)
    city = city_match.group(1).strip() if city_match else 'Rimini'
    return venue, city


def parse_detail(soup, url):
    title_node = soup.select_one('article h1, .wrapper-titolo h1, .wrapper-titolo')
    date_node = soup.select_one('.field--name-field-date-spettacoli')
    title = clean_text(title_node)
    if not title or date_node is None:
        return []

    # The first paragraph contains the occurrence block even on older pages whose
    # malformed markup makes the surrounding Drupal field swallow later content.
    occurrence_node = date_node.select_one('p') or date_node
    occurrence_text = clean_text(occurrence_node)
    occurrences = parse_dates(occurrence_text)
    location = parse_venue(occurrence_node, occurrence_text)
    if not occurrences or not location:
        return []

    description_parts = []
    for node in soup.select(
        'article .field--name-body, article .field--name-field-programma, '
        'article .field--name-field-cast, article .field--name-field-testo'
    ):
        value = clean_text(node)
        if value and value not in description_parts:
            description_parts.append(value)
    description = clean_text('\n\n'.join(description_parts)) or None
    venue, city = location
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'IT',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from in occurrences
    ]


class TeatroGalliItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatrogalli_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        # The origin currently serves an incomplete certificate chain to Requests.
        session.verify = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        feed_urls = {urljoin(SOURCE_URL, path) for path in CURRENT_FEEDS}
        try:
            archive_index = get_soup(session, ARCHIVE_INDEX)
            feed_urls.update(archive_pages(archive_index))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Teatro Galli archive index',
                event='crawler_fetch_failed', level='warning', url=ARCHIVE_INDEX,
                error_type=type(error).__name__, error_message=str(error),
            )

        detail_urls = set()
        for feed_url in sorted(feed_urls):
            try:
                detail_urls.update(event_links(get_soup(session, feed_url)))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Teatro Galli feed',
                    event='crawler_fetch_failed', level='warning', url=feed_url,
                    error_type=type(error).__name__, error_message=str(error),
                )

        records = []

        def fetch_detail(url):
            try:
                return parse_detail(get_soup(session, url), url)
            except (requests.RequestException, TypeError, ValueError) as error:
                log_message(
                    'Failed to parse Teatro Galli event',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                return []

        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(fetch_detail, url): url for url in detail_urls}
            for future in as_completed(futures):
                records.extend(future.result())
        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    TeatroGalliItCrawler().run()


if __name__ == '__main__':
    main()
