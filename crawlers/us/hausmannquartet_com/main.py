import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://hausmannquartet.com/'
SCHEDULE_URL = urljoin(SOURCE_URL, 'schedule/')
SOURCE = 'Hausmann Quartet'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

STATE_PATTERN = (
    r'AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|'
    r'MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|'
    r'TX|UT|VT|VA|WA|WV|WI|WY'
)
CITY_BY_VENUE = {
    'Athenaeum Music & Arts Library': 'La Jolla',
    'Baker-Baum Concert Hall at The Conrad': 'La Jolla',
    'California Surf Museum': 'Oceanside',
    'Fleet Science Center': 'San Diego',
    'Luce Loft': 'San Diego',
    'Maritime Museum of San Diego': 'San Diego',
    'Mingei International Museum': 'San Diego',
    'Moniker Warehouse': 'San Diego',
    'Neil Morgan Auditorium': 'San Diego',
    'North Chapel, Liberty Station': 'San Diego',
    'San Diego Central Library': 'San Diego',
    'San Diego Museum of Art': 'San Diego',
    'Shiley Special Events Suite San Diego Central Library': 'San Diego',
    'Smith Recital Hall': 'San Diego',
    'Smith Recital Hall, SDSU': 'San Diego',
    'SDSU': 'San Diego',
    'SDSU School of Music': 'San Diego',
    'St. Peter\'s Episcopal Church': 'Del Mar',
    'Stone Brewing Liberty Station': 'San Diego',
    'The Conrad: Baker-Baum Concert Hall': 'La Jolla',
    'The Silo Room': 'San Diego',
    'Union Hall Gallery': 'San Diego',
    'Verbatim Books': 'San Diego',
}
ONLINE_VENUES = {'Virtual Concert Hall', 'YouTube stream', 'your home'}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r' *\n+ *', '\n', text).strip()


def parse_datetime(value):
    match = re.fullmatch(
        r'([A-Z][a-z]+ \d{1,2},? \d{4}) at (\d{1,2})(?::(\d{2}))?\s*([ap]m)',
        re.sub(r'\s*\|.*$', '', value).strip(),
        re.I,
    )
    if not match:
        return None
    try:
        date = datetime.strptime(match.group(1).replace(',', ''), '%B %d %Y').date().isoformat()
    except ValueError:
        return None
    hour = int(match.group(2)) % 12 + (12 if match.group(4).lower() == 'pm' else 0)
    return date, f'{hour:02d}:{int(match.group(3) or 0):02d}'


def venue_information(main):
    heading = next(
        (item for item in main.select('.h2') if 'Venue Information' in clean_text(item)),
        None,
    )
    if heading is None:
        return ''
    parts = []
    for item in heading.find_all_next(['p', 'div']):
        if item.name == 'div' and 'h2' in (item.get('class') or []):
            break
        if item.name == 'p':
            text = clean_text(item)
            if text and text not in parts:
                parts.append(text)
    return '\n'.join(parts)


def location_from_text(venue, venue_info):
    combined = '\n'.join(part for part in (venue, venue_info) if part)
    if re.search(r'\bMexico\b', combined, re.I):
        city = next(
            (name for name in ('Ensenada', 'Tijuana') if re.search(rf'\b{name}\b', combined, re.I)),
            None,
        )
        return (city, 'MX') if city else None

    # The quartet's cross-border dates name Tijuana or Ensenada directly even
    # when their older venue block omits the country.
    for city in ('Ensenada', 'Tijuana'):
        if re.search(rf'\b{city}\b', combined, re.I):
            return city, 'MX'

    matches = re.findall(
        rf'(?:^|\n)([A-Za-z .\'-]+),\s*(?:{STATE_PATTERN})(?:\s+\d{{5}}(?:-\d{{4}})?)?\b',
        combined,
        re.M,
    )
    if matches:
        return matches[-1].strip(), 'US'

    # Some archive entries put the city in the venue name but omit an address.
    if venue in CITY_BY_VENUE:
        return CITY_BY_VENUE[venue], 'US'
    for city in ('Berkeley', 'Carlsbad', 'Claremont', 'Columbus',
                 'Escondido', 'Fresno', 'La Jolla', 'Los Angeles', 'San Diego',
                 'Syracuse', 'Torrance'):
        if re.search(rf'\b{re.escape(city)}\b', combined, re.I):
            return city, 'US'
    return None


def record_from_page(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    if main is None:
        return None
    title_parts = [clean_text(item) for item in main.select('.the-jumbotron h1, .the-jumbotron h3')]
    title = ': '.join(item for item in title_parts if item)
    lead = main.select_one('.card .lead')
    lead_text = clean_text(lead)
    parsed = parse_datetime(lead_text)
    venue_match = re.search(r'\|\s*(.+)$', lead_text)
    venue = venue_match.group(1).strip() if venue_match else ''
    if not title or not parsed or not venue or venue in ONLINE_VENUES:
        return None

    location = location_from_text(venue, venue_information(main))
    if not location:
        return None
    city, country_code = location

    content_row = lead.find_parent(class_='card').find_next_sibling(class_='row') if lead else None
    description_column = content_row.select_one('.col-sm-7') if content_row else None
    description = clean_text(description_column) or None
    date, time_from = parsed
    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_record(url):
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return record_from_page(url, response.text)


def scrape_concerts():
    response = requests.get(SCHEDULE_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    urls = list(dict.fromkeys(
        urljoin(SOURCE_URL, anchor.get('href'))
        for anchor in soup.select('a[href*="/event/"]')
        if anchor.get('href')
    ))
    records = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_record, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Hausmann Quartet event',
                    event='crawler_detail_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    unique = {
        (item['title'], item['date'], item['time_from'], item['venue'], item['url']): item
        for item in records
    }
    result = sorted(unique.values(), key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))
    if not result:
        log_message(
            'No valid Hausmann Quartet events found',
            event='crawler_empty_listing',
            level='warning',
            url=SCHEDULE_URL,
            record_count=0,
        )
    return result


class HausmannQuartetComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hausmannquartet_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'url'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    HausmannQuartetComCrawler().run()


if __name__ == '__main__':
    main()
