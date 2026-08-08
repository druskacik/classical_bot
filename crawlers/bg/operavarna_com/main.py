import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operavarna.com/index.php/bg/'
CALENDAR_URL = f'{SOURCE_URL}program-and-tickets'
SOURCE = 'State Opera Varna'
CITY = 'Varna'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'bg-BG,bg;q=0.9,en;q=0.7',
}

# These stages are explicitly listed by the Opera Varna calendar as local.
VARNA_VENUES = {
    'основна сцена', 'лятна сцена зад театъра', 'летен театър',
    'фкц, зала 1', 'градска художествена галерия - варна',
    'концертно студио - радио варна', 'сцена филиал',
    'сцена филиал - камерна сцена', 'пл. "независимост"',
    'дворец на културата и спорта',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=90)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def calendar_months():
    # The site's surviving archive begins in 2013/14. Include one future year
    # because the summer festival is often announced well in advance.
    for year in range(2013, date.today().year + 2):
        for month in range(1, 13):
            yield year, month


def resolve_city(venue):
    normalized = venue.casefold().strip()
    if normalized in VARNA_VENUES or 'варна' in normalized:
        return CITY
    # Touring venues commonly end with an explicit Bulgarian city after a
    # comma or dash. Do not guess when the calendar only names a hall.
    match = re.search(r'(?:,|\s[-–]\s)\s*(?:гр\.?\s*)?([\wА-яЀ-џ .-]+)$', venue)
    if match:
        candidate = clean_text(match.group(1)).strip(' .-')
        if candidate and not re.search(r'\b(?:зала|сцена)\b', candidate, re.I):
            return candidate
    return None


def parse_item(item, year, month):
    title_node = item.select_one('a.name')
    day_node = item.select_one('.day div')
    venue_node = item.select_one('.place')
    if not all((title_node, day_node, venue_node)):
        return None
    title = clean_text(title_node.get_text(' ', strip=True))
    venue = clean_text(venue_node.get_text(' ', strip=True))
    city = resolve_city(venue)
    try:
        event_date = date(year, month, int(clean_text(day_node.get_text()))).isoformat()
    except (TypeError, ValueError):
        return None
    url = title_node.get('href') or ''
    if not url.startswith('http'):
        url = f'{CALENDAR_URL}?{urlencode({"event": url})}' if url else ''
    if not all((title, url, venue, city)):
        return None
    time_text = clean_text(item.select_one('.time').get_text()) if item.select_one('.time') else ''
    times = re.findall(r'(?<!\d)([0-2]\d:[0-5]\d)', time_text)
    description_parts = [
        clean_text(node.get_text('\n', strip=True))
        for node in item.select('.first-line, .categories, .event-cast')
    ]
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': times[0] if times else None,
        'time_to': times[1] if len(times) > 1 else None,
        'venue': venue,
        'city': city,
        'description': '\n'.join(part for part in description_parts if part) or None,
    }


def detail_description(session, url):
    soup = get_soup(session, url)
    parts = []
    for selector in ('.event-heading', '.full-event-data .right', '.anotation', '.event-about'):
        node = soup.select_one(selector)
        text = clean_text(node.get_text('\n', strip=True)) if node else ''
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def scrape_events():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []

    def fetch_month(year, month):
        soup = get_soup(session, CALENDAR_URL, {'month': f'{month:02d}', 'year': year})
        return [
            record for item in soup.select('.event-item')
            if (record := parse_item(item, year, month))
        ]

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(fetch_month, year, month): (year, month)
            for year, month in calendar_months()
        }
        for future in as_completed(futures):
            year, month = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Opera Varna calendar month',
                    event='crawler_page_failed', level='warning', url=CALENDAR_URL,
                    year=year, month=month, error_type=type(error).__name__,
                    error_message=str(error),
                )

    descriptions = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(detail_description, session, url): url for url in {r['url'] for r in records}}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Opera Varna event detail',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
    for record in records:
        record['description'] = descriptions.get(record['url']) or record['description']
    return sorted(records, key=lambda r: (r['date'], r['time_from'] or '', r['title'], r['venue']))


class OperavarnaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operavarna_com', source=SOURCE, source_url=SOURCE_URL,
        country_code='BG', upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'time_to', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_events()


def main():
    OperavarnaComCrawler().run()


if __name__ == '__main__':
    main()
