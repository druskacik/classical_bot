import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://kronosquartet.org/'
EVENTS_API = f'{SOURCE_URL}wp-json/wp/v2/events'
SOURCE = 'Kronos Quartet'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

US_STATES = {
    'Alabama', 'Alaska', 'Arizona', 'Arkansas', 'California', 'Colorado',
    'Connecticut', 'Delaware', 'Florida', 'Georgia', 'Hawaii', 'Idaho',
    'Illinois', 'Indiana', 'Iowa', 'Kansas', 'Kentucky', 'Louisiana', 'Maine',
    'Maryland', 'Massachusetts', 'Michigan', 'Minnesota', 'Mississippi',
    'Missouri', 'Montana', 'Nebraska', 'Nevada', 'New Hampshire', 'New Jersey',
    'New Mexico', 'New York', 'North Carolina', 'North Dakota', 'Ohio',
    'Oklahoma', 'Oregon', 'Pennsylvania', 'Rhode Island', 'South Carolina',
    'South Dakota', 'Tennessee', 'Texas', 'Utah', 'Vermont', 'Virginia',
    'Washington', 'West Virginia', 'Wisconsin', 'Wyoming',
    'District of Columbia',
}

COUNTRY_CODES = {
    'Australia': 'AU', 'Austria': 'AT', 'Belgium': 'BE', 'Brazil': 'BR',
    'Canada': 'CA', 'China': 'CN', 'Czech Republic': 'CZ', 'Czechia': 'CZ',
    'Denmark': 'DK', 'Finland': 'FI', 'France': 'FR', 'Germany': 'DE',
    'Greece': 'GR', 'Hong Kong': 'HK', 'Hungary': 'HU', 'Iceland': 'IS',
    'India': 'IN', 'Ireland': 'IE', 'Israel': 'IL', 'Italy': 'IT',
    'Japan': 'JP', 'Luxembourg': 'LU', 'Mexico': 'MX', 'Netherlands': 'NL',
    'New Zealand': 'NZ', 'Norway': 'NO', 'Poland': 'PL', 'Portugal': 'PT',
    'Singapore': 'SG', 'South Korea': 'KR', 'Spain': 'ES', 'Sweden': 'SE',
    'Switzerland': 'CH', 'Taiwan': 'TW', 'Turkey': 'TR',
    'United Kingdom': 'GB', 'UK': 'GB', 'England': 'GB', 'Scotland': 'GB',
    'Wales': 'GB', 'Northern Ireland': 'GB', 'United States': 'US', 'USA': 'US',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, **kwargs):
    response = session.get(url, timeout=45, **kwargs)
    response.raise_for_status()
    return response


def event_pages(session):
    """Return every published event exposed by the first-party WP API."""
    page = 1
    events = []
    while True:
        response = get_response(
            session,
            EVENTS_API,
            params={
                'per_page': 100,
                'page': page,
                '_fields': 'id,link,slug',
            },
        )
        batch = response.json()
        events.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            return events
        page += 1


def location_fields(location):
    parts = [part.strip() for part in location.rsplit(',', 1)]
    if len(parts) != 2 or not all(parts):
        return None, None
    city, region = parts
    if region in US_STATES:
        return city, 'US'
    return city, COUNTRY_CODES.get(region)


def parse_date(value):
    try:
        return datetime.strptime(value, '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    root = soup.select_one('[data-elementor-type="single"].events')
    if not root:
        return None

    sections = root.select(':scope > section')
    if len(sections) < 2:
        return None
    summary = sections[1]
    heading = summary.select_one('h2.elementor-heading-title')
    location = clean_text(heading)
    city, country_code = location_fields(location)

    summary_text = clean_text(summary)
    date_match = re.search(
        r'\b(January|February|March|April|May|June|July|August|September|October|November|December) '
        r'(\d{1,2}), (20\d{2})\b',
        summary_text,
    )
    event_date = parse_date(date_match.group(0)) if date_match else None
    time_match = re.search(r'\bAT\s+(\d{1,2}):(\d{2})\s*([AP]M)\b', summary_text, re.I)
    time_from = None
    if time_match:
        time_from = datetime.strptime(
            f'{time_match.group(1)}:{time_match.group(2)} {time_match.group(3).upper()}',
            '%I:%M %p',
        ).strftime('%H:%M')

    venue_match = re.search(
        r'\bVENUE:\s*(.*?)(?:\s*\bPRESENTER:|\s*\bBUY TICKETS\b|\s*$)',
        summary_text,
        re.I | re.S,
    )
    venue = clean_text(venue_match.group(1)) if venue_match else ''
    if not location or not event_date or not city or not country_code or not venue:
        return None

    description = None
    if len(sections) >= 3:
        detail_section = sections[2]
        detail_heading = detail_section.find(['h2', 'h3'], string=re.compile(r'^\s*Details\s*$', re.I))
        if detail_heading:
            detail_heading.extract()
        description = clean_text(detail_section) or None

    return {
        'title': location,
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


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for event in event_pages(session):
        url = event.get('link')
        if not url:
            continue
        try:
            response = get_response(session, url)
            record = parse_event(response.text, response.url)
            if record:
                records.append(record)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to scrape Kronos Quartet event',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {(record['url'], record['date']): record for record in records}
    return sorted(unique.values(), key=lambda record: (record['date'], record['time_from'] or '', record['title']))


class KronosQuartetOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kronosquartet_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    return KronosQuartetOrgCrawler().run()


if __name__ == '__main__':
    main()
