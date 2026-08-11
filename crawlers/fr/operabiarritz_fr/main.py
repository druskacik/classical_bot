import re
import unicodedata
from calendar import monthrange
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operabiarritz.fr/'
SOURCE = 'Opéra Biarritz'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}

# The Squarespace navigation has no event collection/API. These labels identify
# its first-party event folders while allowing newly added pages to be found.
EVENT_FOLDER_LABELS = {'programmation', 'archives'}


def make_session():
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    session.headers.update(HEADERS)
    return session


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fold(value):
    return ''.join(
        character for character in unicodedata.normalize('NFKD', value.lower())
        if not unicodedata.combining(character)
    )


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/') or '/', '', ''))


def discover_event_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = set()
    for folder in soup.select('.header-nav-folder-content, .header-menu-nav-folder-content'):
        label = fold(folder.get('data-folder-title', '') or '')
        parent = folder.find_previous(class_=re.compile(r'folder-title|folder-heading'))
        context = label or fold(clean_text(parent))
        if not any(name in context for name in EVENT_FOLDER_LABELS):
            continue
        for link in folder.select('a[href]'):
            url = canonical_url(link.get('href'))
            if urlsplit(url).netloc == urlsplit(SOURCE_URL).netloc:
                urls.add(url)

    # Squarespace's desktop markup does not label folder containers consistently;
    # links between the folder headings are nevertheless marked as folder items.
    for link in soup.select('a.header-nav-folder-item[href], a.header-menu-nav-item[href]'):
        url = canonical_url(link.get('href'))
        if urlsplit(url).netloc == urlsplit(SOURCE_URL).netloc:
            urls.add(url)
    return sorted(urls)


def valid_date(year, month, day):
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_occurrences(text):
    normalized = fold(text)
    occurrences = []

    range_match = re.search(
        r'\bdu\s+(\d{1,2})\s+au\s+(\d{1,2})\s+'
        r'(janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|novembre|decembre)'
        r'\s+(20\d{2})\b', normalized,
    )
    if range_match:
        start, end, month_name, year = range_match.groups()
        month = MONTHS[month_name]
        for day in range(int(start), min(int(end), monthrange(int(year), month)[1]) + 1):
            occurrences.append((date(int(year), month, day), None))
        return occurrences

    pattern = re.compile(
        r'(?<!\d)(\d{1,2})\s+'
        r'(janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|novembre|decembre)'
        r'\s+(20\d{2})(?:\s*[-a]\s*([^\n]{0,35}))?', re.I,
    )
    for match in pattern.finditer(normalized):
        event_date = valid_date(int(match.group(3)), MONTHS[match.group(2)], int(match.group(1)))
        if not event_date:
            continue
        time_text = match.group(4) or ''
        times = re.findall(r'\b([01]?\d|2[0-3])\s*h\s*([0-5]\d)?\b', time_text)
        if not times:
            following = normalized[match.end():match.end() + 55]
            times = re.findall(r'\b([01]?\d|2[0-3])\s*h\s*([0-5]\d)?\b', following)
        if times:
            occurrences.extend(
                (event_date, f'{int(hour):02d}:{minute or "00"}') for hour, minute in times
            )
        else:
            occurrences.append((event_date, None))
    return list(dict.fromkeys(occurrences))


def parse_location(text):
    lines = [clean_text(line) for line in text.splitlines()]
    for cleaned in lines:
        if 'biarritz' not in fold(cleaned):
            continue
        match = re.search(r'^(.+?)\s*[-–]\s*Biarritz\s*$', cleaned, re.I)
        if match and not re.search(r'\b(?:rue|contact|opera de biarritz)\b', fold(match.group(1))):
            return clean_text(match.group(1)), 'Biarritz'
    for cleaned in lines:
        match = re.search(
            r'^(?:a|au)\s+(?:la\s+)?(.+?)\s+(?:de|a)\s+biarritz\s*$',
            fold(cleaned),
        )
        if match:
            venue = match.group(1).strip().title()
            return venue, 'Biarritz'
    return None, None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    if not main:
        return []
    text = clean_text(main)
    occurrences = parse_occurrences(text)
    venue, city = parse_location(text)
    if not occurrences or not venue or not city:
        return []

    heading = main.select_one('h1, h2, h3')
    title = re.sub(r'\s+', ' ', clean_text(heading)).strip()
    if not title:
        title = clean_text(soup.title).split('—', 1)[0].strip()
    if not title:
        return []

    return [{
        'title': title,
        'date': event_date.isoformat(),
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'description': text,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for event_date, event_time in occurrences]


class OperaBiarritzFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operabiarritz_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = make_session()
        response = session.get(SOURCE_URL, timeout=45)
        response.raise_for_status()
        urls = discover_event_urls(response.text)
        records = []
        for url in urls:
            try:
                detail = session.get(url, timeout=45)
                detail.raise_for_status()
                records.extend(parse_event(detail.text, url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Opéra Biarritz detail page',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    OperaBiarritzFrCrawler().run()


if __name__ == '__main__':
    main()
