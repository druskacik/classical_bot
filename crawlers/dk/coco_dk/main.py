import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://coco.dk/'
SOURCE = 'Concerto Copenhagen'
API_URL = 'https://coco.dk/wp-json/tribe/events/v1/events'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'da-DK,da;q=0.9,en;q=0.7',
}

COUNTRY_CODES = {
    'austria': 'AT',
    'belgium': 'BE',
    'canada': 'CA',
    'china': 'CN',
    'denmark': 'DK',
    'danmark': 'DK',
    'finland': 'FI',
    'france': 'FR',
    'germany': 'DE',
    'deutschland': 'DE',
    'hong kong': 'HK',
    'italy': 'IT',
    'ireland': 'IE',
    'japan': 'JP',
    'netherlands': 'NL',
    'holland': 'NL',
    'norway': 'NO',
    'estonia': 'EE',
    'poland': 'PL',
    'spain': 'ES',
    'sweden': 'SE',
    'sverige': 'SE',
    'switzerland': 'CH',
    'slovakia': 'SK',
    'slovenská republika': 'SK',
    'østrig': 'AT',
    'united kingdom': 'GB',
    'uk': 'GB',
    'usa': 'US',
    'united states': 'US',
}

CITY_COUNTRY_CODES = {
    'amsterdam': 'NL',
    'bantry': 'IE',
    'beijing': 'CN',
    'brügge': 'BE',
    'haapsalu': 'EE',
    'innsbruck': 'AT',
    'lund': 'SE',
    'potsdam': 'DE',
    'risør': 'NO',
    'tartu': 'EE',
    'vara': 'SE',
    'zinnowitz': 'DE',
}


def clean_text(value):
    if value is None:
        return ''
    text = html.unescape(str(value))
    if '<' in text and '>' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def country_code(venue):
    country = clean_text(venue.get('country')).casefold()
    if country in COUNTRY_CODES:
        return COUNTRY_CODES[country]

    venue_name = clean_text(venue.get('venue')).casefold()
    for name, code in COUNTRY_CODES.items():
        if re.search(rf'(?:^|[, ]){re.escape(name)}(?:$|[, ])', venue_name):
            return code
    city = clean_text(venue.get('city')).casefold() or (city_name(venue) or '').casefold()
    if city in CITY_COUNTRY_CODES:
        return CITY_COUNTRY_CODES[city]
    return 'DK'


def city_name(venue):
    city = clean_text(venue.get('city'))
    if city:
        if city.casefold() in {'region hovedstaden', 'salen'} or re.search(r'\d', city):
            return None
        return city

    # Older migrated events often store geography only in names such as
    # "Vara Konserthus, Vara, Sverige".  Use only explicit comma components.
    parts = [part.strip() for part in clean_text(venue.get('venue')).split(',') if part.strip()]
    if len(parts) < 2:
        return None
    if parts[-1].casefold() in COUNTRY_CODES:
        parts.pop()
    city = parts[-1] if len(parts) >= 2 else None
    if city:
        city = re.split(r'\s+[–—-]\s+', city, maxsplit=1)[0].strip()
    if (
        not city
        or city.casefold() in COUNTRY_CODES
        or city.casefold() in {'region hovedstaden', 'salen'}
        or re.search(r'\d', city)
    ):
        return None
    return city or None


def venue_name(venue):
    name = clean_text(venue.get('venue'))
    # A few legacy venue records have ticket promotions appended to the name.
    return re.sub(r'\s+[–—-]\s+(?:GRATIS|FREE)\b.*$', '', name, flags=re.IGNORECASE).strip()


def parse_event(event):
    venue = event.get('venue')
    if not isinstance(venue, dict):
        return None
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    venue_name_value = venue_name(venue)
    city = city_name(venue)
    start = clean_text(event.get('start_date'))
    if not all((title, url, venue_name_value, city, start)):
        return None

    try:
        event_date = date.fromisoformat(start[:10]).isoformat()
    except ValueError:
        return None

    time_from = None if event.get('all_day') else start[11:16] or None
    description_parts = [clean_text(event.get('description'))]
    custom_fields = event.get('custom_fields')
    if isinstance(custom_fields, dict):
        for field in custom_fields.values():
            if isinstance(field, dict):
                label = clean_text(field.get('label'))
                value = clean_text(field.get('value'))
                if value:
                    description_parts.append(f'{label}\n{value}' if label else value)
    description = '\n\n'.join(part for part in description_parts if part) or None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue_name_value,
        'city': city,
        'country_code': country_code(venue),
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class CocoDkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='coco_dk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DK',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        params = {
            'per_page': 50,
            'page': 1,
            'start_date': '1990-01-01 00:00:00',
            'end_date': '2100-12-31 23:59:59',
            'status': 'publish',
        }
        records = []
        while True:
            try:
                response = session.get(API_URL, params=params, timeout=45)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Concerto Copenhagen events API',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=params['page'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            for event in payload.get('events', []):
                record = parse_event(event)
                if record:
                    records.append(record)

            total_pages = int(payload.get('total_pages') or 1)
            if params['page'] >= total_pages:
                break
            params['page'] += 1

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    CocoDkCrawler().run()


if __name__ == '__main__':
    main()
