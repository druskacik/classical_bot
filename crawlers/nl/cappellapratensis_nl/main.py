import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.cappellapratensis.nl/'
AGENDA_URL = urljoin(SOURCE_URL, 'nl/AGENDA/')
SOURCE = 'Cappella Pratensis'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}

COUNTRY_CODES = {
    'BE': 'BE',
    'DE': 'DE',
    'EE': 'EE',
    'HR': 'HR',
    'NL': 'NL',
}

# The agenda omits the city for this festival location. Its linked first-party
# festival page identifies the venue as Kloster Knechtsteden in Dormagen.
LOCATION_OVERRIDES = {
    'Festival Alte Musik Knechtsteden': ('Festival Alte Musik Knechtsteden', 'Dormagen'),
}


def clean_text(value, separator=' '):
    if not value:
        return ''
    text = value.get_text(separator, strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    if separator == '\n':
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r' *\n *', '\n', text)
        return re.sub(r'\n{3,}', '\n\n', text).strip()
    return re.sub(r'\s+', ' ', text).strip()


def fetch_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_date_time(value):
    date_match = re.search(r'(\d{2})/(\d{2})/(\d{4})', value)
    if not date_match:
        return None, None
    try:
        parsed = datetime.strptime(date_match.group(0), '%d/%m/%Y')
    except ValueError:
        return None, None
    time_match = re.search(r'\b([01]\d|2[0-3]):[0-5]\d\b', value)
    return parsed.date().isoformat(), time_match.group(0) if time_match else None


def parse_location(value):
    country_match = re.search(r'\(([A-Z]{2})\)\s*$', value)
    country_code = COUNTRY_CODES.get(country_match.group(1), country_match.group(1)) if country_match else 'NL'
    location = re.sub(r'\s*\([A-Z]{2}\)\s*$', '', value).strip()

    if location in LOCATION_OVERRIDES:
        venue, city = LOCATION_OVERRIDES[location]
        return venue, city, country_code

    if ',' not in location:
        return None, None, country_code
    venue, city = (part.strip() for part in location.rsplit(',', 1))
    return venue or None, city or None, country_code


def normalize_url(href):
    # One agenda link currently repeats its absolute URL verbatim.
    absolute_markers = [match.start() for match in re.finditer(r'https?://', href)]
    if len(absolute_markers) > 1:
        href = href[:absolute_markers[1]]
    return urljoin(AGENDA_URL, href)


def detail_description(session, url):
    try:
        soup = fetch_soup(session, url)
    except requests.RequestException as error:
        log_message(
            'Failed to scrape concert detail',
            event='crawler_item_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    content = soup.select_one('main, article, [role="main"]')
    if not content:
        return None
    for node in content.select('script, style, nav, footer, form'):
        node.decompose()
    description = clean_text(content, separator='\n')
    return description or None


def parse_row(row):
    cells = row.find_all('td', recursive=False)
    if len(cells) < 2:
        return None

    date_value = clean_text(cells[0])
    date, time_from = parse_date_time(date_value)
    details = [clean_text(item) for item in cells[1].stripped_strings]
    details = [item for item in details if item]
    if not date or len(details) < 2:
        return None

    location_text = details[0]
    title = clean_text(' '.join(details[1:]))
    venue, city, country_code = parse_location(location_text)
    if not title or not venue or not city:
        return None

    link = row.select_one('a[href]')
    url = normalize_url(link.get('href')) if link else AGENDA_URL
    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    soup = fetch_soup(session, AGENDA_URL)
    records = [record for row in soup.select('main table tr') if (record := parse_row(row))]

    description_urls = {record['url'] for record in records}
    descriptions = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(detail_description, session, url): url for url in description_urls}
        for future in as_completed(futures):
            descriptions[futures[future]] = future.result()
    for record in records:
        record['description'] = descriptions.get(record['url'])

    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title'], record['url']),
    )


class CappellaPratensisNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cappellapratensis_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return get_concerts()


def main():
    CappellaPratensisNlCrawler().run()


if __name__ == '__main__':
    main()
