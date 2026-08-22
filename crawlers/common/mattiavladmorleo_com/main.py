import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mattiavladmorleo.com/'
CONCERTS_URL = f'{SOURCE_URL}concerts'
SOURCE = 'Mattia Vlad Morleo'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_CODES = {
    'Austria': 'AT',
    'Belgium': 'BE',
    'Czech Republic': 'CZ',
    'Denmark': 'DK',
    'France': 'FR',
    'Germany': 'DE',
    'Greece': 'GR',
    'Hungary': 'HU',
    'Ireland': 'IE',
    'Italy': 'IT',
    'Netherlands': 'NL',
    'Norway': 'NO',
    'Poland': 'PL',
    'Portugal': 'PT',
    'Slovakia': 'SK',
    'Spain': 'ES',
    'Sweden': 'SE',
    'Switzerland': 'CH',
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
        return datetime.strptime(value.strip(), '%d.%m.%Y').date().isoformat()
    except (TypeError, ValueError):
        return None


def parse_event(item):
    direct_divs = item.find_all('div', recursive=False)
    if len(direct_divs) < 3:
        return None

    event_date = parse_date(clean_text(direct_divs[0]))
    location_div = direct_divs[2].find('div', recursive=False)
    parts = [clean_text(span) for span in location_div.find_all('span', recursive=False)] if location_div else []
    if len(parts) < 3:
        return None

    city = parts[0].rstrip(' ,')
    country_name = parts[1].strip(' ,')
    venue = re.sub(r'^[\s–—-]+', '', parts[2]).strip()
    # One listing puts the festival name in the city field. The same first-party
    # archive identifies Teatro Kursaal's city as Bari on another occurrence.
    if city == 'Bifest 2026' and venue == 'Teatro Kursaal':
        city = 'Bari'
    country_code = COUNTRY_CODES.get(country_name)
    if not event_date or not city or not venue or not country_code:
        return None

    return {
        'title': f'{SOURCE} concert',
        'date': event_date,
        'url': CONCERTS_URL,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class MattiaVladMorleoComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mattiavladmorleo_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        try:
            response = requests.get(CONCERTS_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Mattia Vlad Morleo concerts',
                event='crawler_fetch_failed',
                level='error',
                url=CONCERTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for item in soup.select('main li'):
            record = parse_event(item)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Mattia Vlad Morleo concert',
                    event='crawler_item_skipped',
                    level='warning',
                    url=CONCERTS_URL,
                    error_type='IncompleteEventData',
                    error_message='Required date, city, venue, or supported country is missing',
                )

        return sorted(
            records,
            key=lambda record: (record['date'], record['city'], record['venue']),
        )


def main():
    MattiaVladMorleoComCrawler().run()


if __name__ == '__main__':
    main()
