import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.reinhold-friedrich.de/'
SOURCE = 'Reinhold Friedrich'
SITEMAP_URL = f'{SOURCE_URL}event-sitemap.xml'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.8',
}

COUNTRY_CODES = {
    'austria': 'AT', 'österreich': 'AT',
    'belgien': 'BE', 'belgium': 'BE',
    'brasilien': 'BR', 'brazil': 'BR',
    'china': 'CN',
    'dänemark': 'DK', 'denmark': 'DK',
    'deutschland': 'DE', 'germany': 'DE',
    'estland': 'EE', 'estonia': 'EE',
    'finnland': 'FI', 'finland': 'FI',
    'frankreich': 'FR', 'france': 'FR',
    'großbritannien': 'GB', 'united kingdom': 'GB', 'uk': 'GB',
    'italien': 'IT', 'italy': 'IT',
    'japan': 'JP',
    'kanada': 'CA', 'canada': 'CA',
    'kroatien': 'HR', 'croatia': 'HR',
    'lettland': 'LV', 'latvia': 'LV',
    'litauen': 'LT', 'lithuania': 'LT',
    'luxemburg': 'LU', 'luxembourg': 'LU',
    'niederlande': 'NL', 'netherlands': 'NL',
    'norwegen': 'NO', 'norway': 'NO',
    'polen': 'PL', 'poland': 'PL',
    'portugal': 'PT',
    'schweden': 'SE', 'sweden': 'SE',
    'schweiz': 'CH', 'switzerland': 'CH', 'suisse': 'CH',
    'slowakei': 'SK', 'slovakia': 'SK',
    'slowenien': 'SI', 'slovenia': 'SI',
    'spanien': 'ES', 'spain': 'ES',
    'südkorea': 'KR', 'south korea': 'KR', 'korea': 'KR',
    'tschechien': 'CZ', 'tschechische republik': 'CZ',
    'czech republic': 'CZ', 'czechia': 'CZ',
    'ungarn': 'HU', 'hungary': 'HU',
    'usa': 'US', 'united states': 'US', 'vereinigte staaten': 'US',
}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = (
        BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
        if '<' in raw
        else raw.strip()
    )
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_country(value):
    normalized = clean_text(value).casefold().rstrip('.,')
    return COUNTRY_CODES.get(normalized)


def parse_city(address_lines):
    if len(address_lines) < 2:
        return ''
    value = clean_text(address_lines[-2])
    value = re.sub(r'^\s*[A-Z]{0,2}[- ]?\d{3,6}\s+', '', value, flags=re.I)
    value = re.sub(r'^\s*\d{3}-\d{4}\s+', '', value)
    value = re.sub(r'^\s*[A-Z]\d[A-Z]\s*\d[A-Z]\d\s+', '', value, flags=re.I)
    return value.strip(' ,')


def event_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    sitemap = BeautifulSoup(response.text, 'xml')
    return [clean_text(node.get_text()) for node in sitemap.select('url > loc')]


def parse_event(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    event = soup.select_one('.em-event-single')
    if not event:
        return None

    title_meta = soup.select_one('meta[property="og:title"]')
    title = clean_text(title_meta.get('content')) if title_meta else ''
    title = re.sub(r'\s+-\s+Reinhold Friedrich$', '', title).strip()

    paragraphs = event.find_all('p', recursive=False)
    date_text = clean_text(paragraphs[0]) if paragraphs else ''
    date_match = re.search(r'\b(\d{1,2}\.\d{1,2}\.\d{4})\b', date_text)
    time_match = re.search(r'\b(\d{1,2}:\d{2})\b', date_text)
    try:
        event_date = datetime.strptime(date_match.group(1), '%d.%m.%Y').date().isoformat()
    except (AttributeError, ValueError):
        return None

    venue_node = event.find('h5')
    venue = clean_text(venue_node) if venue_node else ''
    address_node = venue_node.find_next_sibling('p') if venue_node else None
    address_lines = list(address_node.stripped_strings) if address_node else []
    country_code = parse_country(address_lines[-1]) if address_lines else None
    city = parse_city(address_lines)

    description_parts = []
    if address_node:
        for node in address_node.find_next_siblings():
            if node.name == 'p':
                text = clean_text(node)
                if text and text not in description_parts:
                    description_parts.append(text)

    if not all((title, venue, city, country_code)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_match.group(1) if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event(url, response.text)


class ReinholdFriedrichDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='reinhold_friedrich_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = event_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_event, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                    else:
                        log_message(
                            'Skipped incomplete Reinhold Friedrich event',
                            event='crawler_item_skipped', level='warning', url=url,
                            error_type='IncompleteEventData',
                            error_message='Required title, date, venue, city, or country is missing',
                        )
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Reinhold Friedrich event',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    ReinholdFriedrichDeCrawler().run()


if __name__ == '__main__':
    main()
