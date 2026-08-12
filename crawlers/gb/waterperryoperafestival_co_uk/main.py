import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.waterperryoperafestival.co.uk/'
SOURCE = 'Waterperry Opera Festival'
EVENTS_URL = (
    'https://app.spektrix-link.com/clients/'
    'waterperryoperafestival/eventsView.json'
)
BOX_OFFICE_URL = 'https://purchase.waterperryoperafestival.co.uk/'
CITY = 'Waterperry'
DEFAULT_VENUE = 'Waterperry Gardens'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

# The festival's first-party schedule publishes these production-to-location
# assignments. Unknown additions still have the defensible festival-wide
# Waterperry Gardens venue, rather than being assigned a specific stage.
VENUES = {
    'la bohème': 'Main Stage, Waterperry Gardens',
    'the elixir of love': 'Amphitheatre, Waterperry Gardens',
    'last night at the opera': 'Main Stage, Waterperry Gardens',
    "peter rabbit's musical adventures": 'Amphitheatre, Waterperry Gardens',
    'serenades': 'Garden Glade, Waterperry Gardens',
    'young artist gala': 'Waterperry Ballroom',
    'come & sing workshop': 'Waterperry Ballroom',
    "children's craft workshop": 'The Orangery, Waterperry Gardens',
    'music in the ballroom': 'Waterperry Ballroom',
    'living light': 'Waterperry Church',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_number(item):
    match = re.match(r'\d+', str(item.get('id', '')))
    return match.group(0) if match else None


def venue_for(title):
    normalized = title.lower().replace('’', "'")
    for prefix, venue in VENUES.items():
        if normalized.startswith(prefix):
            return venue
    return DEFAULT_VENUE


def instance_datetimes(item):
    values = []
    for key in ('availableInstanceDates',):
        values.extend(item.get(key) or [])
    # Spektrix omits sold-out and elapsed instances from the available list,
    # but retains the boundary instances. Keep those so archives are not
    # silently lost as performances sell out or pass.
    values.extend([
        item.get('firstInstanceDateTime'),
        item.get('lastInstanceDateTime'),
    ])
    parsed = set()
    for value in values:
        if not value:
            continue
        try:
            parsed.add(datetime.fromisoformat(value).replace(tzinfo=None))
        except ValueError:
            continue
    return sorted(parsed)


def get_concerts():
    response = requests.get(EVENTS_URL, headers=HEADERS, timeout=45)
    response.raise_for_status()
    items = response.json()
    records = []
    for item in items:
        # This stable first-party attribute removes meals, hampers, wine and
        # picnic reservations. It still contains workshops, hence potential.
        if item.get('attribute_IsSupplementary') != 'No':
            continue
        title = clean_text(item.get('name'))
        number = event_number(item)
        if not title or not number:
            continue
        url = f'{BOX_OFFICE_URL}EventAvailability?EventId={number}'
        description = clean_text(
            item.get('htmlDescription') or item.get('description')
        ) or None
        for start in instance_datetimes(item):
            records.append({
                'title': title,
                'date': start.date().isoformat(),
                'url': url,
                'time_from': start.strftime('%H:%M'),
                'venue': venue_for(title),
                'city': CITY,
                'country_code': 'GB',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    unique = {
        (record['url'], record['date'], record['time_from']): record
        for record in records
    }
    result = sorted(
        unique.values(),
        key=lambda record: (
            record['date'], record['time_from'], record['title'], record['url']
        ),
    )
    log_message(
        'Waterperry event feed scraped',
        event='crawler_scrape_completed',
        record_count=len(result),
        url=EVENTS_URL,
    )
    return result


class WaterperryOperaFestivalCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='waterperryoperafestival_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    WaterperryOperaFestivalCoUkCrawler().run()


if __name__ == '__main__':
    main()
