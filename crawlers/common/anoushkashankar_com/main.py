import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://anoushkashankar.com/'
TOUR_URL = 'https://anoushkashankar.com/tour'
SOURCE = 'Anoushka Shankar'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

COUNTRY_CODES = {
    'argentina': 'AR',
    'australia': 'AU',
    'austria': 'AT',
    'belgium': 'BE',
    'brazil': 'BR',
    'canada': 'CA',
    'chile': 'CL',
    'china': 'CN',
    'czech republic': 'CZ',
    'denmark': 'DK',
    'finland': 'FI',
    'france': 'FR',
    'germany': 'DE',
    'hungary': 'HU',
    'india': 'IN',
    'italy': 'IT',
    'japan': 'JP',
    'luxembourg': 'LU',
    'malaysia': 'MY',
    'mexico': 'MX',
    'monaco': 'MC',
    'netherlands': 'NL',
    'portugal': 'PT',
    'scotland': 'GB',
    'singapore': 'SG',
    'slovakia': 'SK',
    'sweden': 'SE',
    'switzerland': 'CH',
    'uae': 'AE',
    'uk': 'GB',
    'united kingdom': 'GB',
    'usa': 'US',
    'wales': 'GB',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip(' ,')


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None


def split_venue_and_programme(value):
    text = clean_text(value)
    match = re.match(r'^(?P<venue>.+?)\s*\((?P<programme>[^()]*)\)\s*$', text)
    if not match:
        return text, None

    venue = clean_text(match.group('venue'))
    programme = clean_text(match.group('programme'))
    programme = re.sub(r'\s*-?\s*Tickets coming soon\s*$', '', programme, flags=re.I)
    return venue, programme or None


def country_code(country, city):
    normalized = clean_text(country).lower()
    if normalized:
        return COUNTRY_CODES.get(normalized)
    # Three archived rows omit country but identify unambiguous city-states.
    city_code = {
        'luxembourg': 'LU',
        'luxumbourg': 'LU',
        'singapore': 'SG',
    }
    if clean_text(city).lower() in city_code:
        return city_code[clean_text(city).lower()]
    return None


def parse_event(item):
    event_date = parse_date(item.select_one('.c-concerts-list__items__item__date'))
    city = clean_text(item.select_one('.c-concerts-list__items__item__location__city'))
    country = clean_text(item.select_one('.c-concerts-list__items__item__location__country'))
    if not city and country.lower() in {'luxembourg', 'monaco', 'singapore'}:
        city = country
    code = country_code(country, city)
    venue, programme = split_venue_and_programme(
        item.select_one('.c-concerts-list__items__item__location__venue')
    )

    if not event_date or not city or not venue or not code:
        return None

    ticket_link = item.select_one('.c-concerts-list__items__item__actions a[href]')
    url = ticket_link.get('href', '').strip() if ticket_link else TOUR_URL
    title = programme or SOURCE
    description = programme

    return {
        'title': title,
        'date': event_date,
        'url': url or TOUR_URL,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class AnoushkaShankarComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='anoushkashankar_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        try:
            response = requests.get(TOUR_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Anoushka Shankar tour page',
                event='crawler_fetch_failed',
                level='error',
                url=TOUR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.select(
            '#concerts-list-current > li.c-concerts-list__items__item, '
            '#concerts-list-past > li.c-concerts-list__items__item'
        )
        records = []
        for item in items:
            record = parse_event(item)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Anoushka Shankar tour entry',
                    event='crawler_item_skipped',
                    level='warning',
                    url=TOUR_URL,
                )

        return sorted(
            records,
            key=lambda record: (record['date'], record['city'], record['venue'], record['title']),
        )


def main():
    AnoushkaShankarComCrawler().run()


if __name__ == '__main__':
    main()
