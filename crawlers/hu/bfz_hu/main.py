import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bfz.hu/hu/'
SOURCE = 'Budapesti Fesztiválzenekar'
API_URL = 'https://www.bfz.hu/ajax/programme/events/monthly/'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'hu-HU,hu;q=0.9,en;q=0.7',
}

# Location modals generally spell out the country for foreign engagements.
# Hungary is commonly omitted from domestic addresses.
COUNTRY_NAMES = {
    'austria': 'AT', 'belgium': 'BE', 'canada': 'CA', 'china': 'CN',
    'croatia': 'HR', 'czech republic': 'CZ', 'czechia': 'CZ',
    'denmark': 'DK', 'france': 'FR', 'germany': 'DE', 'greece': 'GR',
    'hungary': 'HU', 'italy': 'IT', 'japan': 'JP', 'luxembourg': 'LU',
    'netherlands': 'NL', 'norway': 'NO', 'poland': 'PL', 'portugal': 'PT',
    'romania': 'RO', 'slovakia': 'SK', 'slovenia': 'SI', 'south korea': 'KR',
    'spain': 'ES', 'sweden': 'SE', 'switzerland': 'CH', 'taiwan': 'TW',
    'united arab emirates': 'AE', 'united kingdom': 'GB',
    'united states': 'US', 'usa': 'US',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, **kwargs):
    response = session.get(url, timeout=45, **kwargs)
    response.raise_for_status()
    return response


def monthly_events(session, year, month):
    response = get_response(session, API_URL, params={'year': year, 'month': month})
    payload = response.json()
    return payload.get('eventList', [])


def parse_country(address):
    folded = address.casefold().rstrip(' .')
    for name, code in COUNTRY_NAMES.items():
        if re.search(rf'(?:^|[,\s]){re.escape(name)}$', folded):
            return code
    return 'HU'


def parse_location(session, soup):
    node = soup.select_one('.program__venue-medium a[href*="/location/"]')
    if not node:
        node = soup.select_one('.program__venue-mobile a[href*="/location/"]')
    venue_label = clean_text(node)
    if not node or not venue_label:
        return None

    location_url = urljoin(SOURCE_URL, node.get('href'))
    location_soup = BeautifulSoup(get_response(session, location_url).content, 'html.parser')
    address = clean_text(location_soup.select_one('.block__address'))
    modal_title = clean_text(location_soup.select_one('.block__title')) or venue_label

    # BFZ location addresses start with "City - Venue/address". Fall back to
    # the final comma-separated part of the concise modal title.
    city = address.split(' - ', 1)[0].strip() if ' - ' in address else ''
    if not city and ',' in modal_title:
        city = modal_title.rsplit(',', 1)[1].strip()
    venue = modal_title.rsplit(',', 1)[0].strip() if city and modal_title.endswith(city) else modal_title
    if not city or not venue or city.casefold() == venue.casefold():
        return None
    return venue, city, parse_country(address)


def parse_event(session, item):
    url = urljoin(SOURCE_URL, item.get('url', ''))
    match = re.search(r'/(20\d{6})(\d{4})/$', url)
    raw_date = str(item.get('date', ''))
    if not match and not re.fullmatch(r'20\d{6}', raw_date):
        return None
    date_value = raw_date if re.fullmatch(r'20\d{6}', raw_date) else match.group(1)
    try:
        event_date = date.fromisoformat(
            f'{date_value[:4]}-{date_value[4:6]}-{date_value[6:]}'
        ).isoformat()
    except ValueError:
        return None
    time_value = match.group(2) if match else None
    time_from = f'{time_value[:2]}:{time_value[2:]}' if time_value else None

    soup = BeautifulSoup(get_response(session, url).content, 'html.parser')
    title = clean_text(soup.select_one('h1.program__title'))
    location = parse_location(session, soup)
    if not title or not location:
        return None

    description_parts = []
    for selector in (
        '.program__programme .program__section-rich-text',
        '.program__description .block__body',
    ):
        text = clean_text(soup.select_one(selector))
        if text and text not in description_parts:
            description_parts.append(text)
    venue, city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
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
    current_year = date.today().year
    items = {}
    # The endpoint returns empty lists before the site's available archive.
    # Include the full known archive plus announced events two years ahead.
    for year in range(2017, current_year + 3):
        for month in range(1, 13):
            try:
                month_items = monthly_events(session, year, month)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch BFZ calendar month',
                    event='crawler_page_failed', level='warning',
                    url=API_URL, year=year, month=month,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            for item in month_items:
                if item.get('id') and item.get('url'):
                    items[(item['id'], item.get('date'), item['url'])] = item

    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(parse_event, session, item): item for item in items.values()}
        for future in as_completed(futures):
            item = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape BFZ concert',
                    event='crawler_item_failed', level='warning',
                    url=urljoin(SOURCE_URL, item.get('url', '')),
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


class BfzHuCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bfz_hu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='HU',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    BfzHuCrawler().run()


if __name__ == '__main__':
    main()
