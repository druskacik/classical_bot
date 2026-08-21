import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://federicoalbanese.com/'
TOUR_URL = f'{SOURCE_URL}tour'
SOURCE = 'Federico Albanese'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

COUNTRY_CODES = {
    'Australia': 'AU',
    'Austria': 'AT',
    'Belgium': 'BE',
    'Canada': 'CA',
    'Croatia': 'HR',
    'Czech Republic': 'CZ',
    'Denmark': 'DK',
    'Finalnd': 'FI',  # Typo used by the source.
    'France': 'FR',
    'Germany': 'DE',
    'Greece': 'GR',
    'Hungary': 'HU',
    'Iran': 'IR',
    'Ireland': 'IE',
    'Italy': 'IT',
    'Latvia': 'LV',
    'Luxembourg': 'LU',
    'Mexico': 'MX',
    'Nederland': 'NL',
    'Netherlands': 'NL',
    'Norway': 'NO',
    'Poland': 'PL',
    'Portugal': 'PT',
    'Slovakia': 'SK',
    'Spain': 'ES',
    'Sweden': 'SE',
    'Switzerland': 'CH',
    'Turkey': 'TR',
    'UK': 'GB',
    'United Kingdom': 'GB',
    'United States': 'US',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text(' ', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_date(value):
    try:
        return datetime.strptime(value, '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None


def parse_item(item):
    event_date = parse_date(clean_text(item.select_one(
        '.c-concerts-list__items__item__date'
    )))
    city = clean_text(item.select_one(
        '.c-concerts-list__items__item__location__city'
    )).rstrip(',').strip()
    country_name = clean_text(item.select_one(
        '.c-concerts-list__items__item__location__country'
    ))
    venue = clean_text(item.select_one(
        '.c-concerts-list__items__item__location__venue'
    ))
    country_code = COUNTRY_CODES.get(country_name)

    # TBA is not a defensible venue. The dedicated artist calendar supplies no
    # event detail page, title, time, or programme text.
    if not event_date or not city or not country_code or not venue or venue.upper() == 'TBA':
        return None

    ticket_link = item.select_one('.c-concerts-list__items__item__actions a[href]')
    url = ticket_link.get('href', '').strip() if ticket_link else TOUR_URL

    return {
        'title': f'Federico Albanese at {venue}',
        'date': event_date,
        'url': url or TOUR_URL,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class FedericoAlbaneseComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='federicoalbanese_com',
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
        dedupe_subset=['title', 'date', 'venue', 'city'],
    )

    def scrape(self):
        try:
            response = requests.get(TOUR_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Federico Albanese tour calendar',
                event='crawler_fetch_failed',
                level='error',
                url=TOUR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for item in soup.select('.c-concerts-list__items__item'):
            record = parse_item(item)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (record['date'], record['city'], record['venue'], record['url']),
        )


def main():
    FedericoAlbaneseComCrawler().run()


if __name__ == '__main__':
    main()
