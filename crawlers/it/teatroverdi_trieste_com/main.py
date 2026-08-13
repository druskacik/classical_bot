import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.teatroverdi-trieste.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'it/calendario-spettacoli/')
ARCHIVE_URL = urljoin(SOURCE_URL, 'it/spettacoli/')
API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/spettacoli')
SOURCE = 'Teatro Verdi Trieste'
DEFAULT_VENUE = 'Teatro Verdi'
DEFAULT_CITY = 'Trieste'

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


def clean_text(value, separator='\n'):
    if value is None:
        return ''
    text = value.get_text(separator, strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(url):
    last_error = None
    for attempt in range(3):
        try:
            response = requests.get(url, headers=HEADERS, timeout=60)
            response.raise_for_status()
            return BeautifulSoup(response.content, 'html.parser')
        except requests.RequestException as error:
            last_error = error
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise last_error


def event_links(soup):
    links = []
    for anchor in soup.select('a[href*="/it/spettacoli/"]'):
        url = anchor.get('href', '').split('#', 1)[0]
        if not re.search(r'/it/spettacoli/[^/]+/?$', url):
            continue
        if url not in links:
            links.append(url)
    return links


def api_event_links():
    links = []
    page = 1
    while True:
        response = requests.get(
            API_URL,
            headers=HEADERS,
            params={
                'lang': 'it', 'per_page': 100, 'page': page,
                '_fields': 'link,lang',
            },
            timeout=60,
        )
        response.raise_for_status()
        links.extend(item['link'] for item in response.json() if item.get('lang') == 'it')
        if page >= int(response.headers.get('X-WP-TotalPages', page)):
            return list(dict.fromkeys(links))
        page += 1


def parse_occurrences(value):
    text = clean_text(value, ' ').casefold().replace('.', ':')
    text = re.sub(
        r'\b(?:lunedì|martedì|mercoledì|giovedì|venerdì|sabato|domenica)\b',
        ' ', text,
    )
    text = re.sub(r'\be\b', ' ', text)
    if re.search(r'\bdal\b|\bal\b', text):
        return []
    year_match = re.search(r'\b(20\d{2})\b', text)
    if not year_match:
        return []
    year = int(year_match.group(1))
    time_match = re.search(r'\bore\s*(\d{1,2})[:](\d{2})\b', text)
    time_from = None
    if time_match and 0 <= int(time_match.group(1)) <= 23 and int(time_match.group(2)) <= 59:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    occurrences = []
    month_pattern = '|'.join(MONTHS)
    for match in re.finditer(rf'([^a-zà-ÿ]*?)(?:{month_pattern})', text):
        month_word = re.search(rf'({month_pattern})$', match.group(0)).group(1)
        days = [int(item) for item in re.findall(r'\b([0-3]?\d)\b', match.group(1))]
        for day in days:
            try:
                occurrences.append((date(year, MONTHS[month_word], day).isoformat(), time_from))
            except ValueError:
                continue
    return list(dict.fromkeys(occurrences))


def infer_venue(description):
    one_line = clean_text(description, ' ')
    match = re.search(r'(Castello di San Giusto(?:\s*[–-]\s*Cortile delle Milizie)?)', one_line, re.I)
    if match:
        return match.group(1)
    if re.search(r'SALA\s+[“\"]?VICTOR DE SABATA', one_line, re.I):
        return 'Sala Victor de Sabata – Ridotto del Teatro Verdi'
    return DEFAULT_VENUE


def parse_detail(soup, url):
    title = clean_text(soup.select_one('.spettacolo-header-title'), ' ')
    date_text = next(
        (
            clean_text(node, ' ')
            for node in soup.select('.spettacolo-header-small-title')
            if re.search(r'\b20\d{2}\b', clean_text(node, ' '))
        ),
        '',
    )
    if not title or not date_text:
        return []
    occurrences = parse_occurrences(date_text)
    if not occurrences:
        return []

    sections = soup.select('section.spettacolo-block')
    description = clean_text('\n\n'.join(clean_text(section) for section in sections)) or None
    venue = infer_venue(description)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': DEFAULT_CITY,
            'country_code': 'IT',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from in occurrences
    ]


class TeatroverdiTriesteComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatroverdi_trieste_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            links = api_event_links()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Teatro Verdi listings',
                event='crawler_fetch_failed', level='error', url=ARCHIVE_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(get_soup, url): url for url in links}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(parse_detail(future.result(), url))
                except (requests.RequestException, TypeError, ValueError) as error:
                    log_message(
                        'Failed to parse Teatro Verdi event',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    TeatroverdiTriesteComCrawler().run()


if __name__ == '__main__':
    main()
