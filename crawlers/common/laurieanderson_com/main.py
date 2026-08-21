import re
from datetime import datetime

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://laurieanderson.com/'
SOURCE = 'Laurie Anderson'
API_URL = 'https://rest.bandsintown.com/V4/artists/id_35422/events/'
APP_ID = 'js_laurieanderson.com'

COUNTRY_CODES = {
    'Argentina': 'AR',
    'Belgium': 'BE',
    'Canada': 'CA',
    'Chile': 'CL',
    'Croatia': 'HR',
    'Denmark': 'DK',
    'Finland': 'FI',
    'France': 'FR',
    'Germany': 'DE',
    'Iceland': 'IS',
    'Ireland': 'IE',
    'Italy': 'IT',
    'Netherlands': 'NL',
    'Norway': 'NO',
    'Portugal': 'PT',
    'Spain': 'ES',
    'Sweden': 'SE',
    'Switzerland': 'CH',
    'United Kingdom': 'GB',
    'United States': 'US',
}

HEADERS = {
    'Accept': 'application/json',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
}

# A few feed rows put a production or festival name in the venue field. Two
# tour stops can be repaired from addresses that the same feed associates with
# named venues; the others are skipped rather than emitting invented places.
VENUES_BY_ADDRESS = {
    '445 Geary St': 'Curran Theatre',
    '842 S Broadway': 'The Orpheum Theatre',
    'Helsinki Hall of Culture': 'Helsinki Hall of Culture',
}
NON_VENUES = {
    'big ears festival',
    'big ears festival 2026',
    'let x=x',
    'republic of love',
    'rewire',
    'ste there of ithaca, ny',
    'various venues',
}


def clean_text(value):
    if not isinstance(value, str):
        return ''
    return re.sub(r'\s+', ' ', value).strip()


def parse_event(event):
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('name'))
    if venue.casefold() in NON_VENUES:
        venue = VENUES_BY_ADDRESS.get(clean_text(venue_data.get('street_address')), '')
    city = clean_text(venue_data.get('city'))
    country_code = COUNTRY_CODES.get(clean_text(venue_data.get('country')))
    event_id = clean_text(str(event.get('id') or ''))
    start_value = clean_text(event.get('datetime'))

    if not venue or not city or not country_code or not event_id or not start_value:
        return None

    try:
        start = datetime.fromisoformat(start_value.replace('Z', '+00:00'))
    except ValueError:
        return None

    # Bandsintown supplies placeholder clock values for date-only archive rows.
    time_from = None
    if event.get('datetime_display_rule') == 'datetime':
        time_from = start.strftime('%H:%M')

    title = clean_text(event.get('title')) or SOURCE
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': f'https://www.bandsintown.com/e/{event_id}',
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class LaurieAndersonComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='laurieanderson_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        log_message('Fetching Laurie Anderson event feed', event='crawler_url_fetch', url=API_URL)
        try:
            response = requests.get(
                API_URL,
                params={'app_id': APP_ID, 'date': 'all'},
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Laurie Anderson event feed',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        if not isinstance(payload, list):
            raise ValueError('Bandsintown events response was not a list')

        records = []
        skipped = 0
        for event in payload:
            record = parse_event(event) if isinstance(event, dict) else None
            if record:
                records.append(record)
            else:
                skipped += 1

        if skipped:
            log_message(
                'Skipped events without complete date or location data',
                event='crawler_items_skipped',
                level='warning',
                skipped_count=skipped,
            )

        records.sort(
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['url']
            )
        )
        return records


def main():
    LaurieAndersonComCrawler().run()


if __name__ == '__main__':
    main()
