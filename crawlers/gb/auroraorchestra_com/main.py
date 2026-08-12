import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.auroraorchestra.com/'
SOURCE = 'Aurora Orchestra'
LIVE_URL = urljoin(SOURCE_URL, 'live-events/')
ARCHIVE_URL = urljoin(LIVE_URL, 'performance-archive/')
API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/jm_event')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}
COUNTRY_NAMES = {
    'austria': 'AT', 'belgium': 'BE', 'canada': 'CA', 'china': 'CN',
    'czech republic': 'CZ', 'czechia': 'CZ', 'denmark': 'DK', 'finland': 'FI',
    'france': 'FR', 'germany': 'DE', 'hong kong': 'HK', 'ireland': 'IE',
    'italy': 'IT', 'japan': 'JP', 'netherlands': 'NL', 'norway': 'NO',
    'poland': 'PL', 'portugal': 'PT', 'singapore': 'SG', 'south korea': 'KR',
    'spain': 'ES', 'sweden': 'SE', 'switzerland': 'CH', 'taiwan': 'TW',
    'united kingdom': 'GB', 'uk': 'GB', 'united states': 'US', 'usa': 'US',
}
CITY_COUNTRIES = {
    'Amsterdam': 'NL', 'Berlin': 'DE', 'Bremen': 'DE', 'Budapest': 'HU',
    'Cologne': 'DE', 'Dublin': 'IE', 'Frankfurt': 'DE', 'Hamburg': 'DE',
    'Helsinki': 'FI', 'Hong Kong': 'HK', 'Kiel': 'DE', 'Lisbon': 'PT',
    'London': 'GB', 'Luxembourg': 'LU', 'Madrid': 'ES', 'Munich': 'DE',
    'Oslo': 'NO', 'Oxford': 'GB', 'Paris': 'FR', 'Prague': 'CZ',
    'Saffron Walden': 'GB', 'Salzburg': 'AT', 'Seoul': 'KR', 'Shanghai': 'CN',
    'Singapore': 'SG', 'Stockholm': 'SE', 'Tokyo': 'JP', 'Vienna': 'AT',
    'Wiesbaden': 'DE', 'Zurich': 'CH',
}
UK_POSTCODE = re.compile(r'\b(?:GIR ?0AA|[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2})\b', re.I)


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(' ', strip=True)
    else:
        value = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', value.replace('\xa0', ' ')).strip()


def event_links(soup):
    links = set()
    for anchor in soup.select('main a[href*="/event/"]'):
        url = urljoin(SOURCE_URL, anchor.get('href', '')).split('#', 1)[0]
        if re.fullmatch(r'https://www\.auroraorchestra\.com/event/[^/]+/', url):
            links.add(url)
    return links


def archive_pages(soup):
    pages = {ARCHIVE_URL}
    for anchor in soup.select('a[href*="/performance-archive/page/"]'):
        url = urljoin(SOURCE_URL, anchor.get('href', ''))
        if re.fullmatch(r'.*/performance-archive/page/\d+/', url):
            pages.add(url)
    return pages


def post_id(soup):
    classes = soup.body.get('class', []) if soup.body else []
    match = re.search(r'\b(?:postid-|id_)(\d+)\b', ' '.join(classes))
    return int(match.group(1)) if match else None


def description_from_page(soup, acf):
    detail = soup.select_one('.event-detail')
    if detail:
        detail = BeautifulSoup(str(detail), 'html.parser')
        for node in detail.select(
            '.child-events-booking-list, .ticket-button, .social-share, script, style'
        ):
            node.decompose()
    parts = []
    repertoire = clean_text(acf.get('repertoire_listing'))
    artists = clean_text(acf.get('artist_listing'))
    narrative = clean_text(detail)
    if repertoire:
        parts.append(f'Programme: {repertoire}')
    if artists:
        parts.append(f'Artists: {artists}')
    if narrative:
        parts.append(narrative)
    return ' '.join(dict.fromkeys(parts)) or None


def infer_place(address, venue):
    address = clean_text(address)
    venue = clean_text(venue)
    haystack = f'{address}, {venue}'
    country = None
    for name, code in COUNTRY_NAMES.items():
        if re.search(rf'\b{re.escape(name)}\b', haystack, re.I):
            country = code
            break
    if not country and UK_POSTCODE.search(address):
        country = 'GB'

    city = None
    for candidate, code in sorted(CITY_COUNTRIES.items(), key=lambda item: -len(item[0])):
        if re.search(rf'\b{re.escape(candidate)}\b', haystack, re.I):
            city, country = candidate, country or code
            break

    if not city and address:
        parts = [part.strip() for part in address.split(',') if part.strip()]
        usable = []
        for part in parts:
            if any(part.lower() == name for name in COUNTRY_NAMES):
                continue
            stripped = UK_POSTCODE.sub('', part)
            stripped = re.sub(r'^\s*[A-Z-]?\d[\d -]*\s+', '', stripped)
            stripped = re.sub(r'\s+\d{4,6}$', '', stripped).strip(' -')
            if stripped and not re.search(
                r'\b(?:street|st|road|rd|avenue|ave|way|platz|strasse|straße|boulevard|hall)\b',
                stripped, re.I,
            ):
                usable.append(stripped)
        if usable:
            city = usable[-1]
    return city, country


def api_record(payload, page_url, page_title, description):
    acf = payload.get('acf') or {}
    start_raw = acf.get('event_start_date')
    try:
        start = datetime.strptime(start_raw, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None
    venue = clean_text(acf.get('venue_short'))
    if not venue or venue.lower() in {'online', 'various venues', 'various locations'}:
        return None
    city, country = infer_place(acf.get('full_address'), venue)
    if not city or not country:
        return None
    title = clean_text(payload.get('title', {}).get('rendered')) or page_title
    url = payload.get('link') or page_url
    if not all((title, url, venue)):
        return None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def child_records(soup, page_url, description):
    container = soup.select_one('.child-events-booking-list')
    if not container:
        return None
    records = []
    candidates = []
    for block in container.select(':scope > div'):
        paragraph = block.find('p')
        strong = paragraph.find('strong') if paragraph else None
        if paragraph and strong:
            label = clean_text(strong)
            timing = clean_text(paragraph).removeprefix(label).strip()
            candidates.append((label, timing))
    # Older archived tours use plain lines rather than booking cards.
    if not candidates:
        text = clean_text(container)
        pattern = re.compile(
            r'([^()]+?,\s*[^(),]+?,\s*[^(),]+?)\s*'
            r'\(((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{1,2}\s+[A-Za-z]+\s+\d{4})\)',
            re.I,
        )
        candidates.extend((match.group(1).strip(), match.group(2)) for match in pattern.finditer(text))

    for label, timing in candidates:
        label = re.sub(
            r'^(?:Performances in this series:\s*)?'
            r'(?:Previous performances in this tour:\s*)?', '', label, flags=re.I,
        )
        parts = [part.strip().rstrip('‡†*') for part in label.split(',') if part.strip()]
        if len(parts) < 3:
            continue
        title, venue, city = ', '.join(parts[:-2]), parts[-2], parts[-1]
        country = next(
            (code for known_city, code in CITY_COUNTRIES.items()
             if known_city.lower() == city.lower()),
            None,
        )
        match = re.search(
            r'(?:(\d{1,2}(?::|\.)\d{2})\s*(am|pm)?\s*,?\s*)?'
            r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})',
            timing, re.I,
        )
        if not match or not country:
            continue
        try:
            date = datetime.strptime(match.group(3), '%d %B %Y').date().isoformat()
        except ValueError:
            continue
        time_from = None
        if match.group(1):
            raw_time = match.group(1).replace('.', ':') + (match.group(2) or '')
            fmt = '%I:%M%p' if match.group(2) else '%H:%M'
            try:
                time_from = datetime.strptime(raw_time.upper(), fmt).strftime('%H:%M')
            except ValueError:
                pass
        records.append({
            'title': title,
            'date': date,
            'url': page_url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class AuroraOrchestraComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='auroraorchestra_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def fetch_soup(self, session, url):
        response = session.get(url, timeout=45)
        response.raise_for_status()
        return BeautifulSoup(response.text, 'html.parser')

    def api_get(self, session, url, params=None):
        response = session.get(url, params=params, timeout=45)
        response.raise_for_status()
        return response.json()

    def parse_series(self, session, url):
        soup = self.fetch_soup(session, url)
        item_id = post_id(soup)
        if item_id is None:
            return []
        parent = self.api_get(session, f'{API_URL}/{item_id}')
        children = self.api_get(
            session, API_URL,
            params={'parent': item_id, 'per_page': 100, 'orderby': 'date', 'order': 'asc'},
        )
        page_title = clean_text(soup.select_one('main h1'))
        description = description_from_page(soup, parent.get('acf') or {})
        children_from_page = child_records(soup, url, description)
        if children_from_page is not None:
            return children_from_page
        occurrences = children or [parent]
        return [
            record for record in (
                api_record(item, url, page_title, description) for item in occurrences
            ) if record
        ]

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        live = self.fetch_soup(session, LIVE_URL)
        archive = self.fetch_soup(session, ARCHIVE_URL)
        detail_urls = event_links(live)
        pending_pages = archive_pages(archive)
        seen_pages = set()
        while pending_pages:
            page_url = min(pending_pages)
            pending_pages.remove(page_url)
            if page_url in seen_pages:
                continue
            page = archive if page_url == ARCHIVE_URL else self.fetch_soup(session, page_url)
            seen_pages.add(page_url)
            detail_urls.update(event_links(page))
            pending_pages.update(archive_pages(page) - seen_pages)

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(self.parse_series, session, url): url for url in detail_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape Aurora Orchestra event',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        return sorted(records, key=lambda row: (row['date'], row['time_from'], row['title']))


def main():
    AuroraOrchestraComCrawler().run()


if __name__ == '__main__':
    main()
