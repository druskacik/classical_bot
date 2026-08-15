import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wienersymphoniker.at/'
SOURCE = 'Wiener Symphoniker'
CALENDAR_URL = urljoin(SOURCE_URL, 'kalender')
ARCHIVE_URL = urljoin(SOURCE_URL, 'archiv?sort=date_start%3Aasc')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-AT,de;q=0.9,en;q=0.7',
}
COUNTRY_NAMES = {
    'austria': 'AT', 'österreich': 'AT', 'germany': 'DE', 'deutschland': 'DE',
    'italy': 'IT', 'italien': 'IT', 'switzerland': 'CH', 'schweiz': 'CH',
    'france': 'FR', 'frankreich': 'FR', 'spain': 'ES', 'spanien': 'ES',
    'united kingdom': 'GB', 'großbritannien': 'GB', 'netherlands': 'NL',
    'niederlande': 'NL', 'czech republic': 'CZ', 'tschechien': 'CZ',
    'slovakia': 'SK', 'slowakei': 'SK', 'hungary': 'HU', 'ungarn': 'HU',
    'croatia': 'HR', 'kroatien': 'HR', 'slovenia': 'SI', 'slowenien': 'SI',
    'romania': 'RO', 'rumänien': 'RO', 'belgium': 'BE', 'belgien': 'BE',
    'luxembourg': 'LU', 'luxemburg': 'LU', 'japan': 'JP', 'china': 'CN',
    'south korea': 'KR', 'südkorea': 'KR', 'united states': 'US', 'usa': 'US',
}
CITY_COUNTRIES = {
    'Wien': 'AT', 'Vienna': 'AT', 'Bregenz': 'AT', 'Salzburg': 'AT',
    'Graz': 'AT', 'Linz': 'AT', 'Köln': 'DE', 'Cologne': 'DE',
    'München': 'DE', 'Munich': 'DE', 'Berlin': 'DE', 'Hamburg': 'DE',
    'Eindhoven': 'NL', 'Amsterdam': 'NL', 'Antwerpen': 'BE', 'Brüssel': 'BE',
    'Zagreb': 'HR', 'Sofia': 'BG', 'Athen': 'GR', 'Athens': 'GR',
    'Triest': 'IT', 'Trieste': 'IT', 'Madrid': 'ES', 'Barcelona': 'ES',
    'Zaragoza': 'ES', 'Las Palmas de Gran Canaria': 'ES',
    'Santa Cruz de Tenerife': 'ES', 'Paris': 'FR', 'Prag': 'CZ',
    'Prague': 'CZ', 'Budapest': 'HU', 'Bratislava': 'SK', 'London': 'GB',
    'London, UK': 'GB', 'New York': 'US', 'Tokyo': 'JP', 'Osaka': 'JP',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(url):
    parts = urlsplit(url)
    path = re.sub(r'^/index\.php/', '/', parts.path)
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def page_count(soup):
    pages = [0]
    for link in soup.select('nav.pager a[href]'):
        values = parse_qs(urlsplit(link['href']).query).get('page', [])
        if values and values[0].isdigit():
            pages.append(int(values[0]))
    return max(pages) + 1


def listing_urls(html, base_url):
    soup = BeautifulSoup(html, 'html.parser')
    return {
        canonical_url(urljoin(base_url, link['href']))
        for link in soup.select('.s-wsy-entity-booking a[href*="/konzert/"]')
    }


def section_after_heading(soup, heading_text):
    heading = soup.find(
        ['h2', 'h3', 'h4'],
        string=lambda value: value and clean_text(value).casefold() == heading_text.casefold(),
    )
    return heading.parent if heading else None


def parse_city_country(address, venue):
    text = clean_text(address)
    country_code = None
    lowered = text.casefold()
    for name, code in COUNTRY_NAMES.items():
        if re.search(rf'(?:^|[,\n])\s*{re.escape(name)}\s*$', lowered):
            country_code = code
            text = re.sub(rf'(?:^|[,\n])\s*{re.escape(name)}\s*$', '', text, flags=re.I).strip(' ,')
            break

    # Postal addresses consistently put the municipality after the postal code.
    matches = re.findall(r'\b(?:[A-Z]{1,2}-)?\d{4,6}\s+([^,\n]+)', text)
    city = matches[-1].strip() if matches else ''
    city = re.sub(r'\s+(?:Austria|Österreich)$', '', city, flags=re.I).strip()
    if not city:
        # A few historic venue records contain only a municipality and no street.
        pieces = [piece.strip() for piece in re.split(r'[,\n]', text) if piece.strip()]
        if len(pieces) == 1 and not re.search(r'\d', pieces[0]):
            city = pieces[0]

    if country_code is None:
        country_code = CITY_COUNTRIES.get(city)
    if country_code is None:
        if re.search(r'\b(?:A-)?\d{4}\b', text):
            country_code = 'AT'
        elif city in {'Wien', 'Vienna', 'Bregenz', 'Salzburg', 'Graz', 'Linz'}:
            country_code = 'AT'

    if not city:
        venue_lower = venue.casefold()
        for candidate in ('Wien', 'Bregenz', 'Salzburg', 'Graz', 'Linz'):
            if candidate.casefold() in venue_lower:
                city, country_code = candidate, 'AT'
                break
    return city, country_code


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    root = soup.select_one('main .s-wsy-entity-booking.s-full')
    title = soup.select_one('main h1')
    calendar_button = soup.select_one('main [data-ics-start]')
    location = section_after_heading(soup, 'Aufführungsort')
    if not root or not title or not calendar_button or not location:
        return None

    datetime_value = calendar_button.get('data-ics-start', '').strip()
    match = re.match(r'(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}))?', datetime_value)
    if not match:
        return None
    url_date = re.search(r'-(\d{2})-(\d{2})-(\d{4})$', urlsplit(url).path)
    if url_date:
        expected_date = f'{url_date.group(3)}-{url_date.group(2)}-{url_date.group(1)}'
        # Expired occurrence URLs sometimes redirect to another performance of
        # the same production. Never attach that replacement date to the old URL.
        if expected_date != match.group(1):
            return None

    venue_element = location.select_one('.s-accent-text-l')
    address_element = location.select_one('.address')
    venue = clean_text(venue_element)
    city, country_code = parse_city_country(address_element, venue)
    if not venue or not city or not country_code or venue.casefold() == city.casefold():
        return None

    # The complete main booking body retains works, composers, expanded work
    # notes, and editorial copy. Exclude navigation, ticket controls and venue
    # boilerplate while keeping performer context useful to later analysis.
    description_root = BeautifulSoup(str(root), 'html.parser')
    for selector in (
        'script', 'style', 'nav', '.s-booking-jump-menu',
        '.s-booking-sticky-content__wrapper', '.s-wsy-booking__header',
    ):
        for element in description_root.select(selector):
            element.decompose()
    location_copy = section_after_heading(description_root, 'Aufführungsort')
    if location_copy:
        location_copy.decompose()
    description = clean_text(description_root)

    return {
        'title': clean_text(title),
        'date': match.group(1),
        'url': url,
        'time_from': match.group(2),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class WienerSymphonikerCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wienersymphoniker_at',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='AT',
        upload_target='classical',
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def _get(self, url):
        response = requests.get(url, headers=HEADERS, timeout=45)
        response.raise_for_status()
        return response.text

    def _feed_urls(self, feed_url):
        first_html = self._get(feed_url)
        urls = listing_urls(first_html, feed_url)
        count = page_count(BeautifulSoup(first_html, 'html.parser'))
        page_urls = [f'{feed_url}{"&" if "?" in feed_url else "?"}page={page}' for page in range(1, count)]
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(self._get, page_url): page_url for page_url in page_urls}
            for future in as_completed(futures):
                page_url = futures[future]
                try:
                    urls.update(listing_urls(future.result(), page_url))
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Wiener Symphoniker listing page',
                        event='crawler_page_failed', level='warning', url=page_url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        return urls

    def scrape(self):
        event_urls = self._feed_urls(CALENDAR_URL)
        event_urls.update(self._feed_urls(ARCHIVE_URL))
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(self._get, url): url for url in sorted(event_urls)}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = parse_event(future.result(), url)
                    if record:
                        records.append(record)
                    else:
                        log_message(
                            'Skipped incomplete Wiener Symphoniker event',
                            event='crawler_item_skipped', level='warning', url=url,
                        )
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Wiener Symphoniker event detail',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        records.sort(key=lambda item: (item['date'], item['time_from'] or '', item['url']))
        return records


def main():
    WienerSymphonikerCrawler().run()


if __name__ == '__main__':
    main()
