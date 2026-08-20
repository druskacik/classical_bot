import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://anneakikomeyers.com/'
SOURCE = 'Anne Akiko Meyers'
TOUR_URL = urljoin(SOURCE_URL, 'tour-dates/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

US_STATE_CODES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID',
    'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS',
    'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND', 'OH', 'OK',
    'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV',
    'WI', 'WY', 'DC',
}

COUNTRY_NAMES = {
    'Australia': 'AU', 'Austria': 'AT', 'Belgium': 'BE', 'Brazil': 'BR',
    'Canada': 'CA', 'China': 'CN', 'Colombia': 'CO', 'Czech Republic': 'CZ',
    'Denmark': 'DK', 'Finland': 'FI', 'France': 'FR', 'Germany': 'DE',
    'Greece': 'GR', 'Hungary': 'HU', 'Iceland': 'IS', 'Ireland': 'IE',
    'Israel': 'IL', 'Italy': 'IT', 'Japan': 'JP', 'Mexico': 'MX',
    'Netherlands': 'NL', 'New Zealand': 'NZ', 'Norway': 'NO', 'Poland': 'PL',
    'Portugal': 'PT', 'Singapore': 'SG', 'South Korea': 'KR', 'Spain': 'ES',
    'Sweden': 'SE', 'Switzerland': 'CH', 'Taiwan': 'TW', 'United Kingdom': 'GB',
    'United States': 'US', 'USA': 'US',
}

NON_VENUE_PATTERNS = re.compile(
    r'\b(?:amazon|apple\s*music|avie|recording sessions?|sirius\s*xm|spotify|'
    r'streaming|tba|worldwide)\b',
    re.IGNORECASE,
)

KNOWN_US_LOCATIONS = {'Pacific Palisades'}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip(' ,')


def country_code_from_location(location):
    for country_name, country_code in COUNTRY_NAMES.items():
        if re.search(rf'\b{re.escape(country_name)}\b', location, re.IGNORECASE):
            return country_code

    state_match = re.search(r'(?:,|\s)\s*([A-Z]{2})(?:\.|\s|,|$)', location)
    if state_match and state_match.group(1) in US_STATE_CODES:
        return 'US'
    if re.search(r'\b(?:Washington\s*,?\s*D\.?C\.?|District of Columbia)\b', location, re.I):
        return 'US'
    if location.strip() in KNOWN_US_LOCATIONS:
        return 'US'
    if re.search(r'\b(?:B\.?C\.?|British Columbia|Ontario|Quebec|Alberta)\b', location, re.I):
        return 'CA'
    return None


def city_from_location(location, country_code):
    city = re.split(r'\s*,\s*', location, maxsplit=1)[0].strip()
    if country_code == 'CA' and not city:
        return None
    return city or None


def parse_date_and_time(text):
    date_match = re.search(
        r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
        r'([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\b',
        text,
    )
    if not date_match:
        return None, None
    try:
        event_date = datetime.strptime(date_match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None, None

    time_match = re.search(r'\b(\d{1,2})(?::([0-5]\d))?\s*([AP])\.?M\.?(?:\s+[A-Z]{2,5})?\b', text, re.I)
    if not time_match:
        return event_date, None
    hour = int(time_match.group(1)) % 12
    if time_match.group(3).upper() == 'P':
        hour += 12
    return event_date, f'{hour:02d}:{time_match.group(2) or "00"}'


def tour_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = []
    seen = set()
    for link in soup.select('a[href*="/tour-date/"]'):
        url = urljoin(SOURCE_URL, link.get('href', ''))
        parsed = urlparse(url)
        if parsed.netloc != urlparse(SOURCE_URL).netloc or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    page_text = clean_text(soup)
    event_date, time_from = parse_date_and_time(page_text)
    if not event_date:
        return None

    date_node = soup.find(string=re.compile(
        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
        r'[A-Z][a-z]+\s+\d{1,2},\s+\d{4}'
    ))
    header_section = date_node.find_parent('section') if date_node else None
    venue_node = header_section.find('h2') if header_section else None
    venue = clean_text(venue_node)
    location_node = venue_node.find_next(
        'div', class_=lambda value: value and 'elementor-text-editor' in value
    ) if venue_node else None
    location = clean_text(location_node)
    country_code = country_code_from_location(location)
    city = city_from_location(location, country_code) if country_code else None

    title_node = soup.select_one('.tour-date-content .elementor-page-title h2')
    title = clean_text(title_node)
    description_node = soup.select_one('.js-wpv-view-layout .tour-date-content')
    description = clean_text(description_node) or None

    if NON_VENUE_PATTERNS.search(venue) or NON_VENUE_PATTERNS.search(location):
        return None
    if not all((title, venue, city, country_code)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class AnneAkikoMeyersComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='anneakikomeyers_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(TOUR_URL, timeout=60)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Anne Akiko Meyers tour calendar',
                event='crawler_fetch_failed',
                level='error',
                url=TOUR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for url in tour_urls(response.text):
            try:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Anne Akiko Meyers tour detail',
                    event='crawler_detail_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            record = parse_detail(detail_response.text, url)
            if record:
                records.append(record)

        log_message(
            'Anne Akiko Meyers tour scrape completed',
            event='crawler_scrape_completed',
            url=TOUR_URL,
            record_count=len(records),
        )
        return records


def main():
    return AnneAkikoMeyersComCrawler().run()


if __name__ == '__main__':
    main()
