import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.matthiaswinckhler.de/'
AGENDA_URL = f'{SOURCE_URL}agenda'
SOURCE = 'Matthias Winckhler'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.8',
}

# The artist tours internationally, while the first-party calendar supplies only
# a city (not a country). Keep explicit geography for every city it publishes so
# touring appearances are not incorrectly assigned to the artist's home country.
CITY_COUNTRY_CODES = {
    'Amsterdam': 'NL',
    'Barcelona': 'ES',
    'Bochum': 'DE',
    'Bologna': 'IT',
    'Bremen': 'DE',
    'Den Haag': 'NL',
    'Eisenstadt': 'AT',
    'Flensburg': 'DE',
    'Hamburg': 'DE',
    'Middelburg': 'NL',
    'Middelburghu': 'NL',
    'Naarden': 'NL',
    'Paris': 'FR',
    'Salzburg': 'AT',
    'Schwarzenberg': 'DE',
    'Sønderborg': 'DK',
    'Utrecht': 'NL',
    'Wien': 'AT',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_and_time(value):
    text = clean_text(value)
    date_match = re.search(r'\b(\d{2}\.\d{2}\.\d{4})\b', text)
    if not date_match:
        return '', None
    try:
        event_date = datetime.strptime(date_match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return '', None

    time_match = re.search(r'\b(\d{1,2}):(\d{2})\s*Uhr\b', text)
    if not time_match:
        return event_date, None
    hour, minute = (int(part) for part in time_match.groups())
    if hour > 23 or minute > 59:
        return event_date, None
    return event_date, f'{hour:02d}:{minute:02d}'


def parse_event(element):
    title = clean_text(element.select_one('.event__title'))
    date, time_from = parse_date_and_time(element.select_one('.event__date'))
    city = clean_text(element.select_one('.event__city'))
    venue = clean_text(element.select_one('.event__location'))
    description = clean_text(element.select_one('.event__description')) or None
    country_code = CITY_COUNTRY_CODES.get(city)

    if not all((title, date, city, venue, country_code)):
        return None

    return {
        'title': title,
        'date': date,
        'url': AGENDA_URL,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class MatthiasWinckhlerDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='matthiaswinckhler_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(AGENDA_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        records = []
        for element in soup.select('.calendar ul.events > li.event'):
            record = parse_event(element)
            if record:
                records.append(record)
                continue

            city = clean_text(element.select_one('.event__city'))
            log_message(
                'Skipped incomplete Matthias Winckhler event',
                event='crawler_item_skipped',
                level='warning',
                url=AGENDA_URL,
                error_type='IncompleteEventData',
                error_message=(
                    'Required title, valid date, venue, city, or mapped country is missing'
                ),
                city=city,
            )

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    MatthiasWinckhlerDeCrawler().run()


if __name__ == '__main__':
    main()
