import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.asmf.org/'
SOURCE = 'Academy of St Martin in the Fields'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

# The site often gives only a venue name. These are recurring venues for which
# the city is unambiguous; unknown venue-only locations are deliberately skipped.
VENUE_CITIES = {
    'cadogan hall': ('London', 'GB'),
    'church of st martin-in-the-fields': ('London', 'GB'),
    'st martin-in-the-fields': ('London', 'GB'),
    'royal albert hall': ('London', 'GB'),
    "lord's cricket ground": ('London', 'GB'),
    'marylebone cricket club, lord’s cricket ground': ('London', 'GB'),
    'lincoln cathedral': ('Lincoln', 'GB'),
    'hatfield house': ('Hatfield', 'GB'),
    'winona middle school auditorium': ('Winona', 'US'),
    'gerald r. ford amphitheater': ('Vail', 'US'),
}

COUNTRIES = {
    'united kingdom': 'GB', 'uk': 'GB', 'england': 'GB', 'scotland': 'GB',
    'united states': 'US', 'usa': 'US', 'germany': 'DE', 'italy': 'IT',
    'france': 'FR', 'austria': 'AT', 'netherlands': 'NL', 'spain': 'ES',
    'switzerland': 'CH', 'taiwan': 'TW', 'china': 'CN', 'hong kong': 'HK',
    'australia': 'AU', 'canada': 'CA', 'poland': 'PL', 'croatia': 'HR',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value):
    match = re.search(
        r'\b(\d{1,2} [A-Za-z]+ 20\d{2})(?:\s*,\s*(\d{1,2}:\d{2}))?', value
    )
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(1), '%d %B %Y').date().isoformat()
    except ValueError:
        return None, None
    time_from = match.group(2)
    if time_from:
        time_from = datetime.strptime(time_from, '%H:%M').strftime('%H:%M')
    return event_date, time_from


def parse_location(value):
    value = re.sub(r'\s+', ' ', value).strip(' ,')
    if not value:
        return None

    lower = value.lower()
    for venue_name, (city, country_code) in VENUE_CITIES.items():
        if venue_name in lower:
            venue = value.split(',')[0].strip() if ',' in value else value
            return venue, city, country_code

    parts = [part.strip() for part in value.split(',') if part.strip()]
    if len(parts) < 2:
        return None

    country_code = 'GB'
    country_index = None
    for index, part in enumerate(parts):
        code = COUNTRIES.get(part.lower())
        if code:
            country_code = code
            country_index = index
            break

    usable = parts[:country_index] if country_index is not None else parts
    if len(usable) < 2:
        return None
    venue = ', '.join(usable[:-1])
    city = usable[-1]
    if not venue or not city:
        return None
    return venue, city, country_code


def parse_event_page(page_html, url, description):
    soup = BeautifulSoup(page_html, 'html.parser')
    title = clean_text(soup.select_one('h1'))
    if not title:
        return []

    records = []
    for occurrence in soup.select('.events-tickets-list .tour-ticket-item'):
        event_date, time_from = parse_datetime(clean_text(occurrence.select_one('.date')))
        location_element = occurrence.select_one('.day-of-week .block.text-xs')
        location = parse_location(clean_text(location_element))
        if not event_date or not location:
            continue
        venue, city, country_code = location
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description or None,
        })
    return records


class AsmfOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='asmf_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        events = []
        page = 1
        while True:
            try:
                response = session.get(
                    API_URL,
                    params={
                        'per_page': 100,
                        'page': page,
                        '_fields': 'link,title,content',
                    },
                    timeout=45,
                )
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch ASMF event index',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise
            events.extend(response.json())
            if page >= int(response.headers.get('X-WP-TotalPages', page)):
                break
            page += 1

        records = []
        for event in events:
            url = event.get('link')
            if not url:
                continue
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch ASMF event detail',
                    event='crawler_detail_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue

            content_html = event.get('content', {}).get('rendered', '')
            description = clean_text(BeautifulSoup(content_html, 'html.parser')) or None
            records.extend(parse_event_page(response.text, url, description))

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    AsmfOrgCrawler().run()


if __name__ == '__main__':
    main()
