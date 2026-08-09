import re
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lvhf.cz/'
SOURCE = 'Lednicko-valtický hudební festival'
ARCHIVE_URL = urljoin(SOURCE_URL, 'lvhf-archiv/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'cs-CZ,cs;q=0.9,en;q=0.7',
}

# Venue names and venue slugs consistently include their municipality.  The
# festival occasionally performs in Austria, so country is resolved per event.
PLACE_PATTERNS = [
    (r'\b(?:vídeň|vídni|viden|vidni|wien)\b', 'Vídeň', 'AT'),
    (r'\b(?:wilfersdorf)\b', 'Wilfersdorf', 'AT'),
    (r'\b(?:katzelsdorf)\b', 'Katzelsdorf', 'AT'),
    (r'\b(?:poysdorf)\b', 'Poysdorf', 'AT'),
    (r'\b(?:schrattenberg)\b', 'Schrattenberg', 'AT'),
    (r'\b(?:valtice|valticích)\b', 'Valtice', 'CZ'),
    (r'\b(?:lednice|lednici)\b', 'Lednice', 'CZ'),
    (r'\b(?:mikulov)\b', 'Mikulov', 'CZ'),
    (r'\b(?:břeclav|breclav)\b', 'Břeclav', 'CZ'),
    (r'\b(?:hlohovec)\b', 'Hlohovec', 'CZ'),
    (r'\b(?:velké bílovice|velke-bilovice)\b', 'Velké Bílovice', 'CZ'),
    (r'\b(?:moravská nová ves|moravska-nova-ves)\b', 'Moravská Nová Ves', 'CZ'),
    (r'\b(?:praha|prague)\b', 'Praha', 'CZ'),
]


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(url):
    parts = urlsplit(urljoin(SOURCE_URL, url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def page_year(url, fallback_text=''):
    match = re.search(r'20\d{2}', f'{url} {fallback_text}')
    return int(match.group()) if match else None


def discover_listing_pages(session):
    pages = {}
    for index_url in (SOURCE_URL, ARCHIVE_URL):
        soup = get_soup(session, index_url)
        for link in soup.select('a[href]'):
            url = canonical_url(link['href'])
            if not re.search(r'/(?:program-a-vstupenky-20\d{2}|(?:lvhf-)?archiv-koncertu-20\d{2})/?$', url):
                continue
            year = page_year(url, clean_text(link))
            if year:
                pages[url] = year
    return sorted(pages.items(), key=lambda item: item[1], reverse=True)


def discover_events(session):
    events = {}
    for listing_url, year in discover_listing_pages(session):
        try:
            soup = get_soup(session, listing_url)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch LVHF programme page',
                event='crawler_fetch_failed',
                level='warning',
                url=listing_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        for link in soup.select('a[href*="/koncerty/"]'):
            url = canonical_url(link['href'])
            if '/en/koncerty/' in url or '/de/koncerty/' in url:
                continue
            events.setdefault(url, year)
    return events


def parse_date_time(value, year):
    match = re.search(
        r'\b(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{1,2}):([0-5]\d))?', value
    )
    if not match:
        return None, None
    try:
        event_date = date(year, int(match.group(2)), int(match.group(1))).isoformat()
    except ValueError:
        return None, None
    time_from = None
    if match.group(3) is not None and int(match.group(3)) < 24:
        time_from = f'{int(match.group(3)):02d}:{match.group(4)}'
    return event_date, time_from


def detail_value(soup, label):
    for first_cell in soup.select('table.event-details td.first-column'):
        if clean_text(first_cell).casefold() != label.casefold():
            continue
        cells = first_cell.parent.find_all(['td', 'th'], recursive=False)
        return cells[1] if len(cells) > 1 else None
    return None


def parse_location(cell):
    venue = clean_text(cell)
    if not venue:
        return None
    link = cell.select_one('a[href]') if cell else None
    evidence = f'{venue} {link.get("href", "") if link else ""}'.casefold()
    for pattern, city, country_code in PLACE_PATTERNS:
        if re.search(pattern, evidence, re.IGNORECASE):
            return venue, city, country_code
    return None


def extract_description(soup):
    sections = []
    details = soup.select_one('table.event-details')
    if details:
        cloned = BeautifulSoup(str(details), 'html.parser')
        for row in cloned.select('tr'):
            first = row.select_one('.first-column')
            if first and clean_text(first).casefold() in {'kde', 'where'}:
                row.decompose()
        text = clean_text(cloned)
        if text:
            sections.append(text)

    body = soup.select_one('.event-detail-container .row.program')
    if body:
        cloned = BeautifulSoup(str(body), 'html.parser')
        for unwanted in cloned.select('script, style, iframe, .goout-widget, .share, .social-share'):
            unwanted.decompose()
        text = clean_text(cloned)
        if text:
            sections.append(text)
    description = '\n\n'.join(dict.fromkeys(sections))
    return description or None


def parse_event(soup, url, year):
    title = clean_text(soup.select_one('h1'))
    date_element = soup.select_one('.event-date') or soup.select_one('.goout-date')
    event_date, time_from = parse_date_time(clean_text(date_element), year)
    location = parse_location(detail_value(soup, 'Kde'))
    if not title or not event_date or not location:
        return None
    venue, city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': extract_description(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class LvhfCzCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lvhf_cz',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CZ',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for url, year in discover_events(session).items():
            try:
                record = parse_event(get_soup(session, url), url, year)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch LVHF event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping LVHF event with incomplete date or location',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    LvhfCzCrawler().run()


if __name__ == '__main__':
    main()
