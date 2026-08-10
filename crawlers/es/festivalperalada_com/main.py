import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.festivalperalada.com/ca/'
PROGRAM_URL = urljoin(SOURCE_URL, 'programacio/')
SOURCE = 'Festival Perelada'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ca-ES,ca;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def listing_items(session):
    soup = BeautifulSoup(get_response(session, PROGRAM_URL).text, 'html.parser')
    items = []
    seen = set()
    for item in soup.select('li.item'):
        link = item.select_one('a[href*="/programacio/"]')
        date_node = item.select_one('time.date')
        if not link or not date_node:
            continue
        url = urljoin(PROGRAM_URL, link.get('href', ''))
        match = re.search(r'(\d{2})-(\d{2})-(\d{4})', clean_text(date_node))
        if not url or not match or url in seen:
            continue
        try:
            event_date = datetime.strptime(match.group(0), '%d-%m-%Y').date().isoformat()
        except ValueError:
            continue
        seen.add(url)
        items.append((url, event_date))
    return items


def resolve_location(text):
    normalized = clean_text(text).casefold()
    locations = (
        ('palau de la música catalana', 'Palau de la Música Catalana', 'Barcelona'),
        ('auditori parc del castell', 'Auditori Parc del Castell', 'Peralada'),
        ('església del carme', 'Església del Carme', 'Peralada'),
        ('iglesia del carme', 'Església del Carme', 'Peralada'),
        ('mirador del castell', 'Mirador del Castell', 'Peralada'),
        ('castell de peralada', 'Castell de Peralada', 'Peralada'),
    )
    for marker, venue, city in locations:
        if marker in normalized:
            return venue, city
    return None, None


def parse_detail(session, url, event_date):
    soup = BeautifulSoup(get_response(session, url).text, 'html.parser')
    title_node = soup.select_one('.section-header-detail h1') or soup.select_one('h1')
    title = clean_text(title_node)
    description_node = soup.select_one('.corp-background-detail')
    description = clean_text(description_node)
    venue, city = resolve_location(description)
    time_node = soup.select_one('time.date .hour')
    time_match = re.search(r'(?<!\d)(\d{1,2})(?:(?::|\.)\s*(\d{2}))?\s*h', clean_text(time_node))
    time_from = None
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        if hour < 24 and minute < 60:
            time_from = f'{hour:02d}:{minute:02d}'

    if not title or not venue or not city:
        log_message(
            'Skipping event with incomplete required fields',
            event='crawler_item_skipped',
            level='warning',
            url=url,
            missing_title=not bool(title),
            missing_venue=not bool(venue),
            missing_city=not bool(city),
        )
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'ES',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class FestivalPeraladaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festivalperalada_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
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
        items = listing_items(session)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(parse_detail, session, url, event_date): url
                for url, event_date in items
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    FestivalPeraladaComCrawler().run()


if __name__ == '__main__':
    main()
