from datetime import date, datetime
from urllib.parse import urlparse

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.colincurrie.com/'
SOURCE = 'Colin Currie'
EVENTS_URL = (
    'https://feeds.overturehq.com/feeds/'
    'c74b2399/260951/12/performances.json'
)
HEADERS = {
    'Accept': 'application/json',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
}
COUNTRY_CODES = {
    'Czechia': 'CZ',
    'France': 'FR',
    'Germany': 'DE',
    'Luxembourg': 'LU',
    'Netherlands': 'NL',
    'Spain': 'ES',
    'United Kingdom': 'GB',
    'United States': 'US',
}
VENUE_CITY_DEFAULTS = {
    'Theatre on Orlí street': 'Brno',
}


def clean_text(value):
    if value is None or value is False:
        return ''
    return ' '.join(str(value).replace('\xa0', ' ').split())


def valid_url(value):
    value = clean_text(value)
    if not value:
        return ''
    parsed = urlparse(value)
    return value if parsed.scheme in ('http', 'https') and parsed.netloc else ''


def parse_date(value):
    try:
        return date.fromisoformat(clean_text(value)).isoformat()
    except ValueError:
        return ''


def parse_time(value):
    try:
        return datetime.strptime(clean_text(value), '%H:%M').strftime('%H:%M')
    except ValueError:
        return None


def parse_city(value):
    # Overture's `state` field holds the locality, sometimes followed by a
    # state/province (for example "Portland, Oregon").
    return clean_text(value).split(',', 1)[0].strip()


def build_description(event):
    parts = []
    for label, field in (
        ('Subtitle', 'eventSubtitle'),
        ('Presented as', 'bookingName'),
        ('Promoter', 'promoter'),
        ('Artist', 'artist'),
        ('Other artists', 'otherArtists'),
        ('Programme', 'programmeTitle'),
    ):
        value = clean_text(event.get(field))
        if value:
            parts.append(f'{label}: {value}')

    programme = []
    for item in event.get('programme') or []:
        composer = clean_text(item.get('composer'))
        work = clean_text(item.get('work'))
        if composer and work:
            programme.append(f'{composer} — {work}')
        elif composer or work:
            programme.append(composer or work)
    if programme:
        parts.append('Works:\n' + '\n'.join(programme))
    return '\n\n'.join(parts) or None


def parse_event(event):
    title = clean_text(event.get('bookingName')) or clean_text(event.get('eventName'))
    event_date = parse_date(event.get('date'))
    venue = clean_text(event.get('venue'))
    city = parse_city(event.get('state')) or VENUE_CITY_DEFAULTS.get(venue, '')
    country_code = COUNTRY_CODES.get(clean_text(event.get('country')), '')
    url = valid_url(event.get('ticketLink')) or f'{SOURCE_URL}#calendar-section'

    if not all((title, event_date, url, venue, city, country_code)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(event.get('time')),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': build_description(event),
    }


class ColinCurrieComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='colincurrie_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(EVENTS_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError('Expected the Overture performances feed to return a list')

        records = []
        for event in payload:
            record = parse_event(event)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Colin Currie performance',
                    event='crawler_item_skipped',
                    level='warning',
                    url=valid_url(event.get('ticketLink')) or SOURCE_URL,
                    error_type='IncompleteEventData',
                    error_message=(
                        'Required title, date, venue, city, country, or URL is missing'
                    ),
                )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    ColinCurrieComCrawler().run()


if __name__ == '__main__':
    main()
