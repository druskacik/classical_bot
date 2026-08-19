import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.filharmonikusok.hu/'
SOURCE = 'Nemzeti Filharmonikusok'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/concerts'

# This legacy post makes WordPress's ACF REST serializer return HTTP 500.  Its
# date and venue cannot be obtained through the API, so it cannot form a valid
# record.  Excluding it also keeps later pages reachable.
BROKEN_POST_ID = 111736

COUNTRY_CODES = {
    'ausztria': 'AT',
    'austria': 'AT',
    'belgium': 'BE',
    'bosznia-hercegovina': 'BA',
    'china': 'CN',
    'franciaorszag': 'FR',
    'gorogorszag': 'GR',
    'hollandia': 'NL',
    'horvatorszag': 'HR',
    'hungary': 'HU',
    'japan': 'JP',
    'kina': 'CN',
    'lengyelorszag': 'PL',
    'magyarorszag': 'HU',
    'nemetorszag': 'DE',
    'olaszorszag': 'IT',
    'romania': 'RO',
    'rumania': 'RO',
    'spanyolorszag': 'ES',
    'svajc': 'CH',
    'szlovakia': 'SK',
    'szlovenia': 'SI',
    'torokorszag': 'TR',
}

HEADERS = {
    'Accept': 'application/json',
    'User-Agent': 'classical-concert-crawler/1.0 (+https://www.filharmonikusok.hu/)',
}


def hungarian_text(value):
    """Return the Hungarian part of a WPGlobus value and normalize markup."""
    if not value:
        return ''
    value = str(value)
    match = re.search(r'\{:hu\}(.*?)\{:\}', value, re.DOTALL)
    if match:
        value = match.group(1)
    else:
        value = re.sub(r'\{:[a-z]{2}\}.*', '', value, flags=re.DOTALL)
    value = BeautifulSoup(html.unescape(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', value).strip()


def country_code(place, location_category):
    raw_country = hungarian_text(place.get('orszag'))
    key = raw_country.strip().casefold()
    key = (key.replace('á', 'a').replace('é', 'e').replace('í', 'i')
           .replace('ó', 'o').replace('ö', 'o').replace('ő', 'o')
           .replace('ú', 'u').replace('ü', 'u').replace('ű', 'u'))
    if key in COUNTRY_CODES:
        return COUNTRY_CODES[key]
    if location_category in {'budapest', 'videk'}:
        return 'HU'
    return None


def parse_date(value):
    try:
        return datetime.strptime(str(value), '%Y%m%d').date().isoformat()
    except (TypeError, ValueError):
        return None


def parse_time(value):
    match = re.fullmatch(r'([01]?\d|2[0-3]):([0-5]\d)', str(value or '').strip())
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_event(event):
    acf = event.get('acf') or {}
    place = acf.get('esemeny_helyszine') or {}
    title = hungarian_text((event.get('title') or {}).get('rendered'))
    event_date = parse_date(acf.get('esemeny_datum'))
    venue = hungarian_text(place.get('post_title'))
    city = hungarian_text(place.get('varos'))
    code = country_code(place, acf.get('helyszin_kategoria'))
    url = event.get('link')

    if (
        not all((title, event_date, url, venue, city, code))
        or venue.casefold() == city.casefold()
    ):
        log_message(
            'Skipping event with incomplete required fields',
            event='crawler_item_skipped',
            level='warning',
            url=url,
            post_id=event.get('id'),
        )
        return None

    description = hungarian_text((event.get('content') or {}).get('rendered')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(acf.get('esemeny_idopontja')),
        'time_to': parse_time(acf.get('esemeny_vege')),
        'venue': venue,
        'city': city,
        'country_code': code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class FilharmonikusokHuCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filharmonikusok_hu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='HU',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'time_to', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        params = {
            'per_page': 100,
            'page': 1,
            'orderby': 'id',
            'order': 'desc',
            'exclude[]': BROKEN_POST_ID,
            '_fields': (
                'id,link,title,content,acf.esemeny_datum,acf.esemeny_idopontja,'
                'acf.esemeny_vege,acf.esemeny_tipusa,acf.esemeny_helyszine,'
                'acf.helyszin_kategoria'
            ),
        }
        records = []
        total_pages = 1
        while params['page'] <= total_pages:
            response = session.get(API_URL, params=params, timeout=60)
            response.raise_for_status()
            total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            for event in response.json():
                record = parse_event(event)
                if record:
                    records.append(record)
            log_message(
                'Fetched concert API page',
                event='crawler_page_fetched',
                page=params['page'],
                total_pages=total_pages,
                record_count=len(records),
            )
            params['page'] += 1
        return records


def main():
    FilharmonikusokHuCrawler().run()


if __name__ == '__main__':
    main()
