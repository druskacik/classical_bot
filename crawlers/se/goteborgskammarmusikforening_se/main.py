from datetime import datetime
import html
import re

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://goteborgskammarmusikforening.se/'
SOURCE = 'Göteborgs Kammarmusikförening'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
CONCERT_CATEGORY_ID = 8
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'sv-SE,sv;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(html.unescape(str(value)), 'html.parser')
    for element in soup.select(
        'script, style, figure, .tribe-events-schedule, .tribe-block__venue, '
        '.tribe-block__events-link, .wp-block-buttons'
    ):
        element.decompose()
    text = soup.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u00ad', '').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event(event):
    title = clean_text(event.get('title'))
    url = event.get('url')
    start_value = event.get('start_date')
    venue_data = event.get('venue')
    if not isinstance(venue_data, dict):
        return None

    venue = clean_text(venue_data.get('venue'))
    # The association publishes its own concerts in Göteborg. Some venue
    # records omit their city even though their names/addresses are local.
    city = clean_text(venue_data.get('city')) or 'Göteborg'
    if not all((title, url, start_value, venue, city)):
        return None

    try:
        start = datetime.strptime(start_value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None

    description = clean_text(event.get('description')) or None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'SE',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class GoteborgsKammarmusikforeningSeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='goteborgskammarmusikforening_se',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='SE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page = 1

        while True:
            params = {
                'categories': CONCERT_CATEGORY_ID,
                'start_date': '1900-01-01',
                'end_date': '2100-12-31',
                'per_page': 50,
                'page': page,
            }
            try:
                response = session.get(API_URL, params=params, timeout=45)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Göteborgs Kammarmusikförening events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            events = payload.get('events') or []
            for event in events:
                record = parse_event(event)
                if record:
                    records.append(record)

            total_pages = payload.get('total_pages') or 1
            if page >= total_pages:
                break
            page += 1

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    GoteborgsKammarmusikforeningSeCrawler().run()


if __name__ == '__main__':
    main()
