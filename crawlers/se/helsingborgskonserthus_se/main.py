import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.helsingborgskonserthus.se/'
SOURCE = 'Helsingborgs Konserthus'
API_URL = 'https://api.helsingborg.se/event/json/wp/v2/event'
GROUP_ID = 9333
CITY = 'Helsingborg'
VENUE = 'Helsingborgs Konserthus'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'sv-SE,sv;q=0.9,en;q=0.7',
}

# An absent API location normally means the Konserthus itself. Do not apply
# that home-venue default when the event text explicitly advertises a tour.
TOUR_TITLE_RE = re.compile(
    r'\b(?:g[aä]star|konsert\s+i|spelar\s+i|turn[eé])\b.{0,100}\b'
    r'(?:lund|malm[oö]|landskrona|[aå]rhus|g[oö]teborg|stockholm|'
    r'k[oö]penhamn|copenhagen|kristianstad|h[aä]ssleholm)\b',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_events(session):
    page = 1
    events = []
    while True:
        response = session.get(
            API_URL,
            params={
                'user_groups': GROUP_ID,
                'lang': 'sv',
                'per_page': 100,
                'page': page,
                'orderby': 'id',
                'order': 'asc',
            },
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        if not isinstance(batch, list):
            raise ValueError('Event API returned an unexpected response')
        events.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages') or 1)
        if page >= total_pages:
            return events
        page += 1


def event_url(event):
    slug = clean_text(event.get('slug'))
    return urljoin(SOURCE_URL, f'event/{slug}/') if slug else ''


def location_values(value):
    if not isinstance(value, dict):
        return '', ''
    venue = clean_text(value.get('title') or value.get('name'))
    city = clean_text(value.get('city'))
    return venue, city


def resolve_location(event, occasion, title, description):
    venue, city = location_values(occasion.get('location'))
    if not venue and not city:
        venue, city = location_values(event.get('location'))

    if city and city.casefold() != CITY.casefold():
        return (venue or None), city
    if venue or city:
        return venue or VENUE, city or CITY

    if re.search(r'allhelgonakyrkan\s+i\s+lund', title, re.IGNORECASE):
        return 'Allhelgonakyrkan', 'Lund'
    if TOUR_TITLE_RE.search(title):
        return None, None
    return VENUE, CITY


def occasion_record(event, occasion):
    title_data = event.get('title') or {}
    title = clean_text(title_data.get('plain_text') or title_data.get('rendered'))
    content = event.get('content') or {}
    description = clean_text(content.get('plain_text') or content.get('rendered'))
    url = event_url(event)
    start_value = occasion.get('start_date')
    if not all((title, url, start_value)):
        return None
    try:
        start = datetime.strptime(start_value, '%Y-%m-%d %H:%M')
    except (TypeError, ValueError):
        return None

    venue, city = resolve_location(event, occasion, title, description)
    if not venue or not city:
        return None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'SE',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class HelsingborgskonserthusSeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='helsingborgskonserthus_se',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='SE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        events = get_events(session)
        records = []
        for event in events:
            occasions = event.get('all_occasions') or event.get('occasions') or []
            for occasion in occasions:
                record = occasion_record(event, occasion)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped event occurrence with incomplete date or location',
                        event='crawler_item_skipped',
                        level='warning',
                        url=event_url(event),
                    )
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    HelsingborgskonserthusSeCrawler().run()


if __name__ == '__main__':
    main()
