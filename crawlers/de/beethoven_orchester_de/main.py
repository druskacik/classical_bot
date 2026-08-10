import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.beethoven-orchester.de/konzerte/'
BASE_URL = 'https://www.beethoven-orchester.de'
SOURCE = 'Beethoven Orchester Bonn'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

# The calendar describes locations by venue rather than by city. These are the
# touring venues present in its retained season archives; all other listed
# venues are Bonn institutions or local Beethoven Orchester event sites.
TOUR_VENUES = {
    'Anthéa-Antipolis Théâtre Antibes': ('Antibes', 'FR'),
    'Cancarjev Dom': ('Ljubljana', 'SI'),
    'Concertgebouw Amsterdam': ('Amsterdam', 'NL'),
    'Congressforum Frankenthal': ('Frankenthal', 'DE'),
    'Elbphilharmonie': ('Hamburg', 'DE'),
    'Filharmonia Narodowa in Warszawa': ('Warsaw', 'PL'),
    'Helsingborg Konserthus': ('Helsingborg', 'SE'),
    'Karol Szymanowski Philharmonie': ('Kraków', 'PL'),
    'Konzert Theater Coesfeld': ('Coesfeld', 'DE'),
    'Kopenhagen DR Koncerthuset': ('Copenhagen', 'DK'),
    'Kulturzentrum Toblach ( Italien )': ('Toblach', 'IT'),
    'Kurhaus Bad Honnef, Kursaal': ('Bad Honnef', 'DE'),
    'Oslo Konserthus': ('Oslo', 'NO'),
    'Philharmonie Essen': ('Essen', 'DE'),
    'Prinzregententheater': ('Munich', 'DE'),
    'Rhein-Mosel-Halle Koblenz': ('Koblenz', 'DE'),
    'Rhein-Sieg-Halle Siegburg': ('Siegburg', 'DE'),
    'Roncalliplatz Köln': ('Cologne', 'DE'),
    'Warschau': ('Warsaw', 'PL'),
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.content


def normalize_venue(value):
    return re.sub(r'\s+', ' ', clean_text(value)).strip()


def location_for_venue(venue):
    normalized = normalize_venue(venue)
    return TOUR_VENUES.get(normalized, ('Bonn', 'DE'))


def listing_pages(session):
    content = get_page(session, SOURCE_URL)
    soup = BeautifulSoup(content, 'html.parser')
    urls = {SOURCE_URL}
    for link in soup.select('a[href*="/archiv/"]'):
        url = urljoin(BASE_URL, link.get('href', ''))
        if re.search(r'/archiv/\d{2}-\d{2}/?$', urlparse(url).path):
            urls.add(url)
    return urls


def listing_items(content):
    soup = BeautifulSoup(content, 'html.parser')
    items = []
    for element in soup.select('._segment__cpatterns__element[data-uri_details]'):
        detail_path = element.get('data-uri_details', '').strip()
        info = element.select_one('._segment__cpatterns__element__content__info')
        paragraphs = info.find_all('p', recursive=False) if info else []
        if not detail_path or len(paragraphs) < 2:
            continue
        date_text = clean_text(paragraphs[0])
        match = re.search(r'(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}:\d{2}))?', date_text)
        venue = normalize_venue(paragraphs[-1]) if len(paragraphs) >= 4 else ''
        if not match or not venue:
            continue
        try:
            event_date = datetime.strptime(match.group(1), '%d/%m/%Y').date().isoformat()
        except ValueError:
            continue
        items.append({
            'title': clean_text(paragraphs[1]),
            'date': event_date,
            'time_from': match.group(2),
            'venue': venue,
            'url': urljoin(BASE_URL + '/', detail_path),
        })
    return items


def parse_detail(item, content):
    soup = BeautifulSoup(content, 'html.parser')
    title = clean_text(soup.select_one('._segment__cdetails__head__headline'))
    title = re.sub(r'^\d{2}/\d{2}/\d{4}\s*', '', title).strip() or item['title']
    venue = normalize_venue(soup.select_one('._segment__cdetails__meta__location')) or item['venue']
    city, country_code = location_for_venue(venue)

    description_parts = []
    for selector in ('._segment__cdetails__infos', '._segment__cdetails__text'):
        text = clean_text(soup.select_one(selector))
        if text and text not in description_parts:
            description_parts.append(text)

    if not title or not item['date'] or not item['url'] or not venue or not city:
        return None
    return {
        'title': title,
        'date': item['date'],
        'url': item['url'],
        'time_from': item['time_from'],
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items_by_url = {}
    for listing_url in listing_pages(session):
        for item in listing_items(get_page(session, listing_url)):
            items_by_url[item['url']] = item

    records = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {
            executor.submit(get_page, session, url): item
            for url, item in items_by_url.items()
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                record = parse_detail(item, future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=item['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


class BeethovenOrchesterDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='beethoven_orchester_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    BeethovenOrchesterDeCrawler().run()


if __name__ == '__main__':
    main()
