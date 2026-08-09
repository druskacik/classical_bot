import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://mahlerchamber.com/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts')
SOURCE = 'Mahler Chamber Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

COUNTRY_BY_CITY = {
    'Baden-Baden': 'DE', 'Berlin': 'DE', 'Bonn': 'DE', 'Coesfeld': 'DE',
    'Cologne': 'DE', 'Hamburg': 'DE', 'Hitzacker': 'DE', 'Lübeck': 'DE',
    'Wiesbaden': 'DE', 'Lucerne': 'CH', 'Sintra': 'PT', 'London': 'GB',
    'Torroella': 'ES', 'Pollença': 'ES', 'San Sebastián': 'ES',
    'Santander': 'ES', 'Ljubljana': 'SI', 'Cagliari': 'IT', 'Merano': 'IT',
    'Bergamo': 'IT', 'Grafenegg': 'AT', 'Helsinki': 'FI',
    'Palm Springs': 'US', 'Costa Mesa': 'US', 'Santa Barbara': 'US',
    'Northridge': 'US', 'San Francisco': 'US', 'Davis': 'US',
    'Chicago': 'US', 'New York': 'US', 'Chapel Hill': 'US', 'Naples': 'US',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_date_city(value):
    match = re.fullmatch(r'(\d{1,2} [A-Za-z]+ \d{4})\s+(.+)', clean_text(value))
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(1), '%d %B %Y').date().isoformat()
    except ValueError:
        return None, None
    return event_date, match.group(2).strip()


def parse_time_venue(value):
    text = clean_text(value)
    time_match = re.search(r'\b(\d{1,2}:\d{2})(?:am|pm)?\b', text, re.I)
    if not time_match or '/' not in text:
        return None, None
    venue = text.split('/', 1)[1].strip()
    return time_match.group(1), venue or None


def listing_items(session):
    soup = get_soup(session, CONCERTS_URL)
    items = []
    for article in soup.select('article.Concert'):
        link = article.select_one('.Concert-linkLayer-readMore a[href*="/concerts/"]')
        date_node = article.select_one('.Concert-Date')
        place_node = article.select_one('.Concert-Day')
        if not link or not date_node or not place_node:
            continue
        url = urljoin(SOURCE_URL, link.get('href', ''))
        if not re.fullmatch(r'https://mahlerchamber\.com/concerts/\d+', url.rstrip('/')):
            continue
        event_date, city = parse_date_city(date_node)
        time_from, venue = parse_time_venue(place_node)
        country_code = COUNTRY_BY_CITY.get(city)
        if not all((event_date, city, time_from, venue, country_code)):
            log_message(
                'Skipping concert with unresolved required fields',
                event='crawler_item_skipped', level='warning', url=url,
            )
            continue
        summary = clean_text(article.select_one('.Concert-musicians'))
        items.append({
            'url': url, 'date': event_date, 'city': city, 'time_from': time_from,
            'venue': venue, 'country_code': country_code, 'summary': summary,
        })
    return items


def detail_record(session, item):
    soup = get_soup(session, item['url'])
    tour_title = clean_text(soup.select_one('.dtl-Concert-tour_name'))
    summary_lines = [line for line in item['summary'].splitlines() if line]
    concert_title = summary_lines[0] if summary_lines else ''
    title = tour_title
    if concert_title and concert_title.lower() not in tour_title.lower():
        title = f'{tour_title} – {concert_title}' if tour_title else concert_title

    programme = soup.select_one('.Detail-content--concert_program')
    description = clean_text(programme)
    description = re.sub(r'\n(?:SHARE|ADD TO CALENDAR)\b[\s\S]*$', '', description).strip()
    if item['summary'] and item['summary'] not in description:
        description = '\n\n'.join(part for part in (description, item['summary']) if part)
    if not title:
        return None
    return {
        'title': title,
        'date': item['date'],
        'url': item['url'],
        'time_from': item['time_from'],
        'venue': item['venue'],
        'city': item['city'],
        'country_code': item['country_code'],
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = listing_items(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(detail_record, session, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail', event='crawler_item_failed',
                    level='warning', url=item['url'], error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(records, key=lambda row: (row['date'], row['time_from'], row['url']))


class MahlerchamberComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mahlerchamber_com',
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
    MahlerchamberComCrawler().run()


if __name__ == '__main__':
    main()
