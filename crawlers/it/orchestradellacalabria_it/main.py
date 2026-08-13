import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.orchestradellacalabria.it/'
EVENTS_API = f'{SOURCE_URL}wp-json/wp/v2/eventi'
SOURCE = 'Orchestra Filarmonica della Calabria'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(r'\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})\b', value)
    if not match:
        return None
    month = MONTHS.get(match.group(2).casefold())
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2})[:.](\d{2})\b', value)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def parse_location(value):
    value = clean_text(value)
    parts = [part.strip(' ,') for part in re.split(r'\s+[–—|]\s+', value) if part.strip(' ,')]
    if len(parts) < 2:
        return None

    venue_words = re.compile(
        r'\b(?:teatro|chiesa|palazzo|anfiteatro|auditorium|arena|basilica|'
        r'cattedrale|castello|conservatorio|sala|campus|santuario|piazza|duomo)\b',
        re.I,
    )
    left, right = parts[0], parts[-1]
    if venue_words.search(left) and not venue_words.search(right):
        venue, city_text = left, right
    elif venue_words.search(right):
        venue, city_text = right, left
    else:
        return None

    country_code = 'IT'
    foreign_countries = {'tunisia': 'TN'}
    if city_text.casefold() in foreign_countries:
        country_code = foreign_countries[city_text.casefold()]
        city_match = re.search(r'\b(?:di|de|del)\s+(.+)$', venue, re.I)
        if not city_match:
            return None
        city_text = city_match.group(1)
    city_text = re.sub(r'\s*\([A-Z]{2}\)\s*$', '', city_text, flags=re.I)
    city_text = re.sub(r',\s*(?:Italia|Italy)\s*$', '', city_text, flags=re.I)
    city = city_text.strip(' ,')
    if not venue or not city or venue.casefold() == city.casefold():
        return None
    return venue, city, country_code


def fetch_event(item, session=requests):
    url = item.get('link', '')
    response = session.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')

    title = clean_text(item.get('title', {}).get('rendered'))
    metadata = [clean_text(node) for node in soup.select('.events')]
    event_date = next((parsed for value in metadata if (parsed := parse_date(value))), None)
    time_from = next((parsed for value in metadata if (parsed := parse_time(value))), None)
    location = next((parsed for value in metadata if (parsed := parse_location(value))), None)
    if not title or not event_date or not location:
        return None

    venue, city, country_code = location
    description = clean_text(BeautifulSoup(
        item.get('content', {}).get('rendered', ''), 'html.parser'
    )) or None
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


class OrchestradellacalabriaItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestradellacalabria_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        try:
            items = []
            page = 1
            while True:
                response = requests.get(
                    EVENTS_API,
                    headers=HEADERS,
                    params={
                        'per_page': 100,
                        'page': page,
                        'orderby': 'date',
                        'order': 'asc',
                        '_fields': 'id,link,title,content',
                    },
                    timeout=45,
                )
                response.raise_for_status()
                items.extend(response.json())
                total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
                if page >= total_pages:
                    break
                page += 1
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Orchestra della Calabria event API',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_API,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        session = requests.Session()
        session.headers.update(HEADERS)
        for item in items:
            try:
                record = fetch_event(item, session)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Orchestra della Calabria event',
                    event='crawler_item_failed',
                    level='warning',
                    url=item.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda row: (
                row['date'], row['time_from'] or '', row['title'], row['venue'], row['city']
            ),
        )


def main():
    OrchestradellacalabriaItCrawler().run()


if __name__ == '__main__':
    main()
