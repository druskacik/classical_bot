import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bochumer-symphoniker.de/'
PROGRAM_URL = urljoin(SOURCE_URL, 'programm')
EVENTS_API = f'{SOURCE_URL}?type=1691066967'
SOURCE = 'Bochumer Symphoniker'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.mount('https://', HTTPAdapter(pool_connections=24, pool_maxsize=24))

MONTHS = {
    'Jan': 1, 'Feb': 2, 'März': 3, 'Mrz': 3, 'Apr': 4, 'Mai': 5,
    'Juni': 6, 'Juli': 7, 'Aug': 8, 'Sept': 9, 'Sep': 9, 'Okt': 10,
    'Nov': 11, 'Dez': 12,
}

# Tour venues normally include their city in the displayed venue name. These
# aliases cover frequent exceptions while avoiding a false Bochum default.
CITY_ALIASES = {
    'bochum': 'Bochum', 'dortmund': 'Dortmund', 'essen': 'Essen',
    'duisburg': 'Duisburg', 'düsseldorf': 'Düsseldorf', 'koeln': 'Köln',
    'köln': 'Köln', 'bonn': 'Bonn', 'hamburg': 'Hamburg', 'berlin': 'Berlin',
    'münchen': 'München', 'frankfurt': 'Frankfurt am Main',
    'amsterdam': 'Amsterdam', 'concertgebouw': 'Amsterdam',
    'kölner philharmonie': 'Köln', 'konzerthaus dortmund': 'Dortmund',
}
HOME_VENUES = ('großer saal', 'kleiner saal', 'foyer', 'musikforum')


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(r'(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\s+(\d{2,4})', value)
    if not match or match.group(2) not in MONTHS:
        return None
    year = int(match.group(3))
    if year < 100:
        year += 2000
    try:
        return date(year, MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None


def month_payload(year, month):
    fields = {
        'detailPage': '16', 'month': str(month), 'year': str(year),
        'page': '1', 'viewType': 'calendar',
    }
    # The API treats the presence of this field as the archive switch. It is
    # required for past months and harmless for future months.
    if (year, month) < (date.today().year, date.today().month):
        fields['archive'] = 'true'
    return {key: (None, value) for key, value in fields.items()}


def fetch_month(year, month):
    response = SESSION.post(EVENTS_API, files=month_payload(year, month), timeout=45)
    response.raise_for_status()
    return response.json().get('events') or ''


def listing_items(html):
    items = []
    soup = BeautifulSoup(html, 'html.parser')
    for teaser in soup.select('a.event-teaser-s[href]'):
        title_node = teaser.select_one('.subline')
        date_node = teaser.select_one('.headline')
        if not title_node or not date_node:
            continue
        date_text = clean_text(date_node)
        event_date = parse_date(date_text)
        url = urljoin(SOURCE_URL, teaser.get('href'))
        times = re.findall(r'\b([01]\d|2[0-3]):([0-5]\d)\b', date_text)
        if not event_date or not url:
            continue
        for hour, minute in times or [(None, None)]:
            items.append({
                'title': clean_text(title_node),
                'date': event_date,
                'url': url,
                'time_from': f'{hour}:{minute}' if hour else None,
            })
    return items


def detail_data(url):
    response = SESSION.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    venue_node = soup.select_one('#cheroTeaser .teaser-box .richtext p')
    venue = clean_text(venue_node)
    city = None
    lower_venue = venue.lower()
    for alias, resolved_city in CITY_ALIASES.items():
        if alias in lower_venue:
            city = resolved_city
            break
    if not city and any(name in lower_venue for name in HOME_VENUES):
        city = 'Bochum'

    description_parts = []
    for heading in soup.select('h2.headline span'):
        label = clean_text(heading)
        if label not in ('Programm', 'Beschreibung'):
            continue
        text_container = heading.find_parent(class_='text')
        body = text_container.select_one('.richtext') if text_container else None
        body_text = clean_text(body)
        if body_text and body_text not in description_parts:
            description_parts.append(f'{label}\n{body_text}')
    return venue, city, clean_text('\n\n'.join(description_parts)) or None


def get_concerts():
    today = date.today()
    # The selectable public archive begins with season 2014/15. Include it all,
    # plus an 18-month horizon so newly announced future seasons are discovered.
    last_month = today.month + 18
    last_year = today.year + (last_month - 1) // 12
    last_month = (last_month - 1) % 12 + 1
    months = []
    year, month = 2014, 7
    while (year, month) <= (last_year, last_month):
        months.append((year, month))
        month += 1
        if month == 13:
            year, month = year + 1, 1

    items = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_month, year, month): (year, month) for year, month in months}
        for future in as_completed(futures):
            year, month = futures[future]
            try:
                items.extend(listing_items(future.result()))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape programme month', event='crawler_page_failed',
                    level='warning', url=EVENTS_API, year=year, month=month,
                    error_type=type(error).__name__, error_message=str(error),
                )

    details = {}
    urls = sorted({item['url'] for item in items})
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(detail_data, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                details[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail', event='crawler_item_failed',
                    level='warning', url=url, error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = []
    for item in items:
        venue, city, description = details.get(item['url'], (None, None, None))
        if not item['title'] or not venue or not city:
            continue
        records.append({
            **item, 'venue': venue, 'city': city, 'country_code': 'DE',
            'description': description, 'source_url': SOURCE_URL, 'source': SOURCE,
        })
    return sorted(records, key=lambda record: (
        record['date'], record['time_from'] or '', record['title'], record['url']
    ))


class BochumerSymphonikerDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bochumer_symphoniker_de', source=SOURCE, source_url=SOURCE_URL,
        country_code='DE', upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    BochumerSymphonikerDeCrawler().run()


if __name__ == '__main__':
    main()
