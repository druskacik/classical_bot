import math
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://markusposchner.de/'
CALENDAR_URL = f'{SOURCE_URL}calender/'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Markus Poschner'
PAGE_SIZE = 50

# The public calendar currently starts in 2024. A fixed wide interval keeps its
# available archive as well as announced future seasons in the crawl.
START_DATE = '2000-01-01 00:00:00'
END_DATE = '2100-12-31 23:59:59'

HEADERS = {
    'Accept': 'application/json',
    'Referer': CALENDAR_URL,
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

COUNTRY_CODES = {
    'Austria': 'AT',
    'Croatia (Local Name: Hrvatska)': 'HR',
    'Czech Republic': 'CZ',
    'France': 'FR',
    'Germany': 'DE',
    'Japan': 'JP',
    'Netherlands': 'NL',
    'Slovenia': 'SI',
    'Switzerland': 'CH',
    'United States': 'US',
}

# Many older venue records omit their country. The calendar is a touring
# conductor's international schedule, so infer only from the supplied city.
CITY_COUNTRY_CODES = {
    'Amsterdam': 'NL', 'Antwerpen': 'BE', 'Bad Kissingen': 'DE',
    'Basel': 'CH', 'Berlin': 'DE', 'Bremen': 'DE', 'Bruxelles': 'BE',
    'Dallas': 'US', 'Dornbirn': 'AT', 'Dresden': 'DE',
    'Düsseldorf': 'DE', 'Ebensee': 'AT', 'Freiburg im Breisgau': 'DE',
    'Friedrichshafen': 'DE', 'Gent': 'BE', 'Gmunden': 'AT',
    'Graz': 'AT', 'Hamburg': 'DE', 'Ichikawa': 'JP', 'Kochi': 'JP',
    'Linz': 'AT', 'Ljubljana': 'SI', 'Locarno': 'CH', 'Lugano': 'CH',
    'Luzern': 'CH', 'Mannheim': 'DE', 'Mülheim an der Ruhr': 'DE',
    'München': 'DE', 'Nogata': 'JP', 'Nürnberg': 'DE', 'Ogden': 'US',
    'Orem': 'US', 'Paris': 'FR', 'Park City': 'US',
    'Praha 1-Vinohrady': 'CZ', 'Příbram VII': 'CZ',
    'Salt Lake City': 'US', 'Salzburg': 'AT', 'Sion': 'CH',
    'St. Florian': 'AT', 'Strasbourg': 'FR', 'Stuttgart': 'DE',
    'Takamatsu': 'JP', 'Villach': 'AT', 'Wakayama': 'JP', 'Wien': 'AT',
    'Zagreb': 'HR', 'Zürich': 'CH',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def clean_description(value):
    if not value:
        return None
    soup = BeautifulSoup(str(value), 'html.parser')
    for element in soup.select(
        'script, style, iframe, .tribe-events-schedule, '
        '.tribe-block__event-price, .tribe-block__venue, '
        '.tribe-block__organizer'
    ):
        element.decompose()
    return clean_text(soup.get_text('\n', strip=True)) or None


def parse_start(value, all_day=False):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None, None
    return parsed.date().isoformat(), None if all_day else parsed.strftime('%H:%M')


def resolve_location(venue):
    if not isinstance(venue, dict):
        return None
    venue_name = clean_text(venue.get('venue'))
    city = clean_text(venue.get('city'))

    # This venue is stored with the country name in the API's city field.
    if 'halle aux grains' in venue_name.casefold() and city == 'Frankreich':
        city = 'Toulouse'

    country = clean_text(venue.get('country'))
    country_code = COUNTRY_CODES.get(country) or CITY_COUNTRY_CODES.get(city)
    if not venue_name or not city or not country_code:
        return None
    return venue_name, city, country_code


def make_record(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    event_date, time_from = parse_start(
        event.get('start_date'), bool(event.get('all_day'))
    )
    location = resolve_location(event.get('venue'))
    if not title or not url or not event_date or not location:
        return None
    venue, city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_description(event.get('description')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_page(session, page):
    response = session.get(
        API_URL,
        params={
            'per_page': PAGE_SIZE,
            'page': page,
            'start_date': START_DATE,
            'end_date': END_DATE,
            'status': 'publish',
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


class MarkusposchnerDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='markusposchner_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        first_page = fetch_page(session, 1)
        events = list(first_page.get('events') or [])
        page_count = int(first_page.get('total_pages') or 0)
        if not page_count:
            page_count = math.ceil(int(first_page.get('total') or 0) / PAGE_SIZE)

        for page in range(2, page_count + 1):
            try:
                events.extend(fetch_page(session, page).get('events') or [])
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Markus Poschner calendar page',
                    event='crawler_page_failed',
                    level='warning',
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        records = []
        for event in events:
            record = make_record(event)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Markus Poschner calendar event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=clean_text(event.get('url')),
                    error_type='IncompleteEventData',
                    error_message=(
                        'Required title, date, URL, venue, city, or country is missing'
                    ),
                )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    MarkusposchnerDeCrawler().run()


if __name__ == '__main__':
    main()
