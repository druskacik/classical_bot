import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://aam.co.uk/'
SOURCE = 'Academy of Ancient Music'
FEED_URLS = (
    urljoin(SOURCE_URL, 'whats-on/'),
    urljoin(SOURCE_URL, 'whats-on-past/'),
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{1,2}\s+'
    r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{2}\b'
)
TIME_RE = re.compile(r'\b(\d{1,2})[.:](\d{2})\s*(am|pm)\b', re.IGNORECASE)

COUNTRY_NAMES = {
    'Austria': 'AT',
    'Estonia': 'EE',
    'France': 'FR',
    'Germany': 'DE',
    'Italy': 'IT',
    'Netherlands': 'NL',
    'Romania': 'RO',
    'Spain': 'ES',
    'Switzerland': 'CH',
    'United Kingdom': 'GB',
}
FOREIGN_CITIES = {
    'Amsterdam': 'NL',
    'Berlin': 'DE',
    'Cologne': 'DE',
    'Groningen': 'NL',
    'Hannover': 'DE',
    'Las Palmas': 'ES',
    'Tallinn': 'EE',
    'Tartu': 'EE',
    'Venice': 'IT',
    'Weisbaden': 'DE',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(value)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(0), '%a %d %b %y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'pm':
        hour += 12
    return f'{hour:02d}:{int(match.group(2)):02d}'


def parse_city_country(value):
    city = value.strip(' ,')
    if not city:
        return None
    if ',' in city:
        candidate, country = (part.strip() for part in city.rsplit(',', 1))
        country_code = COUNTRY_NAMES.get(country)
        if country_code and candidate:
            return candidate, country_code
    return city, FOREIGN_CITIES.get(city, 'GB')


def extract_description(soup, first_occurrence):
    parts = []
    for element in soup.select('.ct-text-block'):
        if first_occurrence and element is first_occurrence:
            break
        text = clean_text(element)
        if len(text) >= 60 and text not in parts:
            parts.append(text)

    programme_heading = next(
        (element for element in soup.select('.ct-text-block')
         if clean_text(element).upper() == 'PROGRAMME'),
        None,
    )
    if programme_heading:
        programme = programme_heading.find_next(class_='oxy-dynamic-list')
        programme_text = clean_text(programme)
        if programme_text:
            parts.append(f'PROGRAMME\n{programme_text}')
    return '\n\n'.join(parts) or None


def parse_detail(html, url, title):
    soup = BeautifulSoup(html, 'html.parser')
    city_elements = soup.select('.concert-single-city')
    first_occurrence = city_elements[0].parent.parent if city_elements else None
    description = extract_description(soup, first_occurrence)
    records = []

    for city_element in city_elements:
        location = parse_city_country(clean_text(city_element))
        occurrence = city_element.parent.parent
        event_date = parse_date(clean_text(occurrence))
        venue_element = city_element.find_previous_sibling(class_='ct-text-block')
        venue = clean_text(venue_element)
        if not location or not event_date or not venue:
            continue
        city, country_code = location
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(clean_text(occurrence)),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def fetch_detail(item):
    url, title = item
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    response.encoding = 'utf-8'
    return parse_detail(response.text, url, title)


class AamCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='aam_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        events = {}
        session = requests.Session()
        session.headers.update(HEADERS)
        for feed_url in FEED_URLS:
            try:
                response = session.get(feed_url, timeout=45)
                response.raise_for_status()
                response.encoding = 'utf-8'
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch AAM concert listing',
                    event='crawler_fetch_failed',
                    level='error',
                    url=feed_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise
            soup = BeautifulSoup(response.text, 'html.parser')
            for card in soup.select('.aam-concert-card'):
                link = card.select_one('a[href*="/concerts/"]')
                title = clean_text(card.select_one('.whatson-title'))
                if link and title:
                    events[urljoin(feed_url, link.get('href', ''))] = title

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(fetch_detail, item): item[0]
                for item in events.items()
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to parse AAM concert detail',
                        event='crawler_detail_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'],
                record['venue'], record['city'],
            ),
        )


def main():
    AamCoUkCrawler().run()


if __name__ == '__main__':
    main()
