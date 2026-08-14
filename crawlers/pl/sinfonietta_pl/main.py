from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sinfonietta.pl/'
SOURCE = 'Sinfonietta Cracovia'
CALENDAR_URL = urljoin(SOURCE_URL, 'kalendarium')
ARCHIVE_URL = urljoin(SOURCE_URL, 'archiwum-koncertow')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}
COUNTRIES = {
    'austria': 'AT', 'belgia': 'BE', 'belgium': 'BE', 'czechy': 'CZ',
    'france': 'FR', 'francja': 'FR', 'germany': 'DE', 'hiszpania': 'ES',
    'italia': 'IT', 'italy': 'IT', 'niemcy': 'DE', 'niderlandy': 'NL',
    'holandia': 'NL', 'litwa': 'LT', 'poland': 'PL', 'polska': 'PL',
    'portugalia': 'PT', 'slovakia': 'SK', 'słowacja': 'SK',
    'spain': 'ES', 'szwajcaria': 'CH', 'united kingdom': 'GB', 'węgry': 'HU',
    'stany zjednoczone': 'US',
}
FOREIGN_CITIES = {
    'berlin': 'DE', 'bratysława': 'SK', 'bratislava': 'SK', 'budapeszt': 'HU',
    'budapest': 'HU', 'londyn': 'GB', 'london': 'GB', 'praga': 'CZ', 'prague': 'CZ',
    'wiedeń': 'AT', 'vienna': 'AT', 'wilno': 'LT', 'zurich': 'CH', 'zürich': 'CH',
    'alicante': 'ES', 'alacant': 'ES', 'amsterdam': 'NL', 'bilbao': 'ES',
    'breda': 'NL', 'gandawa': 'BE', 'gent': 'BE', 'kroměříž': 'CZ',
    'la laguna': 'ES', 'madryt': 'ES', 'new canaan': 'US', 'new york': 'US',
    'porto': 'PT',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\u200d', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_location(soup, venue):
    section = soup.select_one('#Section-wskazowki-dojazdu')
    address = clean_text(section.select_one('p.body-1')) if section else ''
    country_code = 'PL'
    folded = f'{address} {venue}'.casefold()
    for name, code in COUNTRIES.items():
        if re.search(rf'(?<!\w){re.escape(name)}(?!\w)', folded):
            country_code = code
            break

    city = ''
    postal = re.search(r'\b\d{2}-\d{3}\s+([^,;\n]+)', address)
    if postal:
        city = postal.group(1).strip()
    if not city:
        for candidate, code in FOREIGN_CITIES.items():
            if re.search(rf'(?<!\w){re.escape(candidate)}(?!\w)', folded):
                city = candidate.title()
                country_code = code
                break
    if not city:
        # Common European forms: "03010 Alacant", "1019 BR Amsterdam",
        # "4149-071 Porto", and their comma-separated continuations.
        postal_city = re.search(
            r'\b(?:\d{4,5}(?:-\d{3})?|\d{4}\s+[A-Z]{2}|\d{3}\s+\d{2})\s+'
            r'([^,;\n\d]+)', address,
        )
        if postal_city:
            city = postal_city.group(1).strip()
    if not city and re.search(r'\b\d{2}-\d{3}\s*$', address):
        # A few Polish records put the city first and the postcode last.
        city = address.split(',', 1)[0].strip()
    if not city:
        locality = re.search(r'(?:^|,\s*)([\wÀ-ɏ .’-]+)\s*$', address)
        if locality and not re.search(r'\d', locality.group(1)):
            city = locality.group(1).strip()
    if not city and ',' in address:
        first_part = address.split(',', 1)[0].strip()
        if first_part and not re.search(r'\d', first_part):
            city = first_part
    if not city:
        known_home_venues = (
            'Nowohuckie Centrum Kultury', 'Teatr Łaźnia Nowa',
        )
        if any(name.casefold() in venue.casefold() for name in known_home_venues):
            city = 'Kraków'
        elif 'Opera Bałtycka' in venue:
            city = 'Gdańsk'
    if not city and re.search(r'krak(?:\u00f3|o)w|cracov', f'{address} {venue}', re.I):
        city = 'Kraków'
    return city, country_code


def section_text(soup, selector, label):
    section = soup.select_one(selector)
    if not section:
        return ''
    body = section.select_one('.w-richtext')
    text = clean_text(body)
    return f'{label}\n{text}' if text else ''


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1'))
    date_box = soup.select_one('.concert-main-date.for-mobile') or soup.select_one('.concert-main-date')
    date_match = re.search(r'\b(\d{1,2})\s+(\d{1,2})\s+(\d{4})\b', clean_text(date_box))
    event_date = ''
    if date_match:
        try:
            event_date = date(
                int(date_match.group(3)), int(date_match.group(2)), int(date_match.group(1))
            ).isoformat()
        except ValueError:
            pass
    time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', clean_text(date_box))
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    venue = clean_text(soup.select_one('.concert-main-place'))
    venue = re.sub(r'\nMUZYCZNE MOSTY$', '', venue, flags=re.I)
    if re.search(r'\b(?:lokalizacj|locations?)', venue, re.I):
        return None
    city, country_code = parse_location(soup, venue)
    description_parts = [
        section_text(soup, '#Section-program', 'Program'),
        section_text(soup, '#Section-o-koncercie', 'O koncercie'),
        section_text(soup, '#Section-artysci', 'Wystąpią'),
    ]
    description = '\n\n'.join(part for part in description_parts if part) or None
    if not all((title, event_date, url, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class SinfoniettaPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sinfonietta_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def _get_soup(self, session, url):
        response = session.get(url, headers=HEADERS, timeout=45)
        response.raise_for_status()
        return BeautifulSoup(response.text, 'html.parser')

    def _event_urls(self, session):
        urls = set()
        calendar = self._get_soup(session, CALENDAR_URL)
        urls.update(urljoin(CALENDAR_URL, tag['href']) for tag in calendar.select('a[href*="/koncerty/"]'))

        page = 1
        while True:
            url = ARCHIVE_URL if page == 1 else f'{ARCHIVE_URL}?bef42d70_page={page}'
            soup = self._get_soup(session, url)
            urls.update(urljoin(url, tag['href']) for tag in soup.select('a[href*="/koncerty/"]'))
            if not soup.select_one('a.w-pagination-next'):
                break
            page += 1
        return sorted(urls)

    def _fetch_event(self, session, url):
        response = session.get(url, headers=HEADERS, timeout=45)
        response.raise_for_status()
        return parse_event_page(response.text, url)

    def scrape(self):
        session = requests.Session()
        urls = self._event_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(self._fetch_event, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                    else:
                        log_message(
                            'Skipped incomplete Sinfonietta Cracovia event',
                            event='crawler_item_skipped', level='warning', url=url,
                            error_type='IncompleteEventData',
                            error_message='Required title, date, venue, or city is missing',
                        )
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Sinfonietta Cracovia event',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    SinfoniettaPlCrawler().run()


if __name__ == '__main__':
    main()
