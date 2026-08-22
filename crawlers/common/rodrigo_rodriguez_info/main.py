import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://rodrigo-rodriguez.info/'
SOURCE = 'Rodrigo Rodriguez'
EVENTS_URL = f'{SOURCE_URL}#events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

# The artist tours internationally, so geography must come from each card.
# Add cities here only when the card itself names them unambiguously.
CITY_COUNTRIES = {
    'london': ('London', 'GB'),
    'madrid': ('Madrid', 'ES'),
    'tokyo': ('Tokyo', 'JP'),
    'moscow': ('Moscow', 'RU'),
    'new york': ('New York', 'US'),
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
    match = re.search(
        r'\b(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\s*,?\s*(20\d{2})\b',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(2).lower()], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None


def parse_location(title, description):
    combined = f'{title}\n{description}'
    city_match = None
    city_data = None
    for key, value in CITY_COUNTRIES.items():
        match = re.search(rf'\b{re.escape(key)}\b', combined, re.IGNORECASE)
        if match and (city_match is None or match.start() < city_match.start()):
            city_match = match
            city_data = value
    if city_data is None:
        return None

    city, country_code = city_data
    # Cards currently put the venue immediately before the city. Requiring
    # that structure prevents countries, addresses, and prose becoming venues.
    venue_match = re.search(
        rf'(?:^|\n)([^\n,]+),\s*{re.escape(city)}\b', combined, re.IGNORECASE
    )
    if not venue_match:
        return None
    venue = venue_match.group(1).strip(' ,-')
    if not venue or venue.casefold() == city.casefold():
        return None
    return venue, city, country_code


def parse_event(card):
    title = clean_text(card.select_one('.et_pb_module_header'))
    description = clean_text(card.select_one('.et_pb_blurb_description')) or None
    event_date = parse_date(title)
    location = parse_location(title, description or '')
    if not title or not event_date or not location:
        return None

    venue, city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': EVENTS_URL,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
    }


class RodrigoRodriguezInfoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='rodrigo_rodriguez_info',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        dedupe_subset=['title', 'date', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        try:
            response = requests.get(SOURCE_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Rodrigo Rodriguez events',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        events_section = soup.select_one('#events')
        if events_section is None:
            raise ValueError('Events section was not found')

        records = []
        for card in events_section.select('.et_pb_blurb'):
            record = parse_event(card)
            if record:
                records.append(record)
        return sorted(records, key=lambda item: (item['date'], item['title']))


def main():
    RodrigoRodriguezInfoCrawler().run()


if __name__ == '__main__':
    main()
