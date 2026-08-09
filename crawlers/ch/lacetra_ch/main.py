import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://lacetra.ch/'
CONCERTS_URL = urljoin(SOURCE_URL, 'en/concerts/')
SOURCE = 'La Cetra Barockorchester & Vokalensemble Basel'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9,de-CH;q=0.8',
}
MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}
COUNTRY_CODES = {
    'schweiz': 'CH', 'switzerland': 'CH', 'suisse': 'CH',
    'deutschland': 'DE', 'germany': 'DE',
    'luxembourg': 'LU', 'luxemburg': 'LU',
    'nederland': 'NL', 'netherlands': 'NL', 'niederlande': 'NL',
    'france': 'FR', 'frankreich': 'FR',
    'italia': 'IT', 'italy': 'IT', 'italien': 'IT',
    'österreich': 'AT', 'austria': 'AT',
}
CITY_COUNTRIES = {
    'Amsterdam': 'NL',
    'Luxembourg': 'LU',
    'Regensburg': 'DE',
}


def clean_text(value):
    if not value:
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


def parse_start(value):
    match = re.search(
        r'\b(\d{1,2})\.\s+([A-Za-z]+)\s+(\d{4})\s+(\d{1,2}):(\d{2})\b',
        value,
    )
    if not match:
        return None
    day, month_name, year, hour, minute = match.groups()
    month = MONTHS.get(month_name.casefold())
    if not month:
        return None
    try:
        return datetime(int(year), month, int(day), int(hour), int(minute))
    except ValueError:
        return None


def discover_cards(session):
    cards = []
    seen_urls = set()
    url = CONCERTS_URL

    while url:
        soup = get_soup(session, url)
        for link in soup.select('a[href*="/en/concerts/"]'):
            date_node = link.select_one('.konzertdatum')
            location_node = link.select_one('.konzertort')
            title_node = link.select_one('h2')
            detail_url = link.get('href')
            if not all((date_node, location_node, title_node, detail_url)):
                continue
            detail_url = urljoin(SOURCE_URL, detail_url)
            if detail_url in seen_urls:
                continue
            seen_urls.add(detail_url)
            cards.append({
                'url': detail_url,
                'title': clean_text(title_node),
                'start': parse_start(clean_text(date_node)),
                'location': clean_text(location_node),
            })

        next_link = next(
            (link for link in soup.select('a[href]')
             if clean_text(link).casefold() == 'more concerts'),
            None,
        )
        next_url = urljoin(SOURCE_URL, next_link['href']) if next_link else None
        url = next_url if next_url and next_url != url else None

    return cards


def split_location(value):
    parts = [part.strip() for part in value.rsplit(',', 1)]
    if len(parts) != 2 or not all(parts):
        return None, None
    venue, city = parts
    if venue.casefold() == city.casefold():
        return None, None
    return venue, city


def detail_data(session, card):
    soup = get_soup(session, card['url'])
    entry = soup.select_one('article .entry-content')
    if not entry:
        return None, None

    location = entry.select_one('.konzertort')
    location_text = clean_text(location)
    country_code = CITY_COUNTRIES.get(split_location(card['location'])[1])
    for country_name, code in COUNTRY_CODES.items():
        if re.search(rf'\b{re.escape(country_name)}\b', location_text, re.I):
            country_code = code
            break
    country_code = country_code or 'CH'

    for node in entry.select(
        '.konzertdatum, .konzertort, .konzertreihe, .konzertsolist, '
        '.backbutton, script, style, img, figure'
    ):
        node.decompose()
    description = clean_text(entry)
    return country_code, description or None


class LacetraChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lacetra_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        dedupe_subset=['url'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        cards = discover_cards(session)
        records = []

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(detail_data, session, card): card for card in cards}
            for future in as_completed(futures):
                card = futures[future]
                venue, city = split_location(card['location'])
                if not all((card['title'], card['start'], venue, city)):
                    continue
                try:
                    country_code, description = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape La Cetra concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=card['url'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                records.append({
                    'title': card['title'],
                    'date': card['start'].date().isoformat(),
                    'url': card['url'],
                    'time_from': card['start'].strftime('%H:%M'),
                    'venue': venue,
                    'city': city,
                    'country_code': country_code,
                    'description': description,
                })

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'], item['title'], item['venue']
        ))


def main():
    LacetraChCrawler().run()


if __name__ == '__main__':
    main()
