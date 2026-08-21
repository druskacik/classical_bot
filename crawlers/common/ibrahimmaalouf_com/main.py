from datetime import datetime
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ibrahimmaalouf.com/'
SOURCE = 'Ibrahim Maalouf'
ARTIST_NAME = 'Ibrahim Maalouf'
APP_ID = 'js_emailwidget_0000174188'
API_URL = (
    f'https://rest.bandsintown.com/artists/{quote(ARTIST_NAME)}/events'
)

COUNTRY_CODES = {
    'armenia': 'AM',
    'australia': 'AU',
    'austria': 'AT',
    'belgium': 'BE',
    'canada': 'CA',
    'czech republic': 'CZ',
    'czechia': 'CZ',
    'denmark': 'DK',
    'egypt': 'EG',
    'finland': 'FI',
    'france': 'FR',
    'germany': 'DE',
    'greece': 'GR',
    'hong kong': 'HK',
    'hungary': 'HU',
    'india': 'IN',
    'italy': 'IT',
    'japan': 'JP',
    'lebanon': 'LB',
    'luxembourg': 'LU',
    'monaco': 'MC',
    'morocco': 'MA',
    'netherlands': 'NL',
    'norway': 'NO',
    'poland': 'PL',
    'portugal': 'PT',
    'réunion': 'RE',
    'romania': 'RO',
    'saudi arabia': 'SA',
    'serbia': 'RS',
    'slovakia': 'SK',
    'slovenia': 'SI',
    'south africa': 'ZA',
    'south korea': 'KR',
    'spain': 'ES',
    'sweden': 'SE',
    'switzerland': 'CH',
    'taiwan': 'TW',
    'thailand': 'TH',
    'the netherlands': 'NL',
    'tunisia': 'TN',
    'turkey': 'TR',
    'türkiye': 'TR',
    'united arab emirates': 'AE',
    'united kingdom': 'GB',
    'united states': 'US',
}

HEADERS = {
    'Accept': 'application/json',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
}


def clean_text(value):
    return ' '.join(str(value or '').replace('\xa0', ' ').split())


def canonical_event_url(value):
    parts = urlsplit(clean_text(value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def parse_event(event):
    venue_data = event.get('venue') or {}
    title = clean_text(event.get('title')) or ARTIST_NAME
    venue = clean_text(venue_data.get('name'))
    city = clean_text(venue_data.get('city'))
    country_name = clean_text(venue_data.get('country')).casefold()
    country_code = COUNTRY_CODES.get(country_name)
    url = canonical_event_url(event.get('url'))

    try:
        occurrence = datetime.fromisoformat(clean_text(event.get('datetime')))
    except (TypeError, ValueError):
        occurrence = None

    # Some ticket-imported rows put the show title in the venue field.  It is
    # not a defensible venue, so omit those occurrences rather than publishing
    # descriptive text as a location.
    venue_is_event_title = bool(event.get('title')) and venue.casefold() == title.casefold()

    if venue_is_event_title or not all(
        (title, occurrence, url, venue, city, country_code)
    ):
        return None

    description = clean_text(event.get('description')) or None
    return {
        'title': title,
        'date': occurrence.date().isoformat(),
        'url': url,
        'time_from': occurrence.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class IbrahimMaaloufComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ibrahimmaalouf_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        response = requests.get(
            API_URL,
            params={'app_id': APP_ID, 'date': 'all'},
            headers=HEADERS,
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError('Bandsintown API returned an unexpected response')

        records = []
        for event in payload:
            record = parse_event(event)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Ibrahim Maalouf event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=canonical_event_url(event.get('url')) or API_URL,
                    error_type='IncompleteEventData',
                    error_message=(
                        'Required title, date, URL, venue, city, or country is missing'
                    ),
                )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    IbrahimMaaloufComCrawler().run()


if __name__ == '__main__':
    main()
