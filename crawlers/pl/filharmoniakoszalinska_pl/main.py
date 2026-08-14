import re
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://filharmoniakoszalinska.pl/'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Filharmonia Koszalińska im. Stanisława Moniuszki'
DEFAULT_CITY = 'Koszalin'
DEFAULT_VENUE = SOURCE

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}

# The API sometimes leaves the city field blank even though the venue name
# gives it unambiguously in the Polish locative case.
CITY_FORMS = {
    'Bobolicach': 'Bobolice',
    'Darłowie': 'Darłowo',
    'Jamnie': 'Koszalin',
    'Kołobrzegu': 'Kołobrzeg',
    'Koszalinie': 'Koszalin',
    'Łobzie': 'Łobez',
    'Mielnie': 'Mielno',
    'Rewalu': 'Rewal',
    'Sarbinowie': 'Sarbinowo',
    'Sianowie': 'Sianów',
    'Siemczynie': 'Siemczyno',
    'Sławnie': 'Sławno',
    'Świdwinie': 'Świdwin',
    'Ustroniu Morskim': 'Ustronie Morskie',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def city_from_text(text):
    for form, city in sorted(CITY_FORMS.items(), key=lambda item: -len(item[0])):
        if re.search(r'\b' + re.escape(form) + r'\b', text, re.I):
            return city
    return None


def parse_event(item):
    title = clean_text(item.get('title'))
    url = (item.get('url') or '').strip()
    start = (item.get('start_date') or '').strip()
    try:
        event_date = date.fromisoformat(start[:10]).isoformat()
    except (TypeError, ValueError):
        return None

    venue_data = item.get('venue') if isinstance(item.get('venue'), dict) else {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    if venue and not city:
        city = city_from_text(venue) or city_from_text(title)

    # An event without a venue is normally an agency rental in the philharmonic
    # hall. Do not apply the home default when its title explicitly identifies a
    # touring city; those records need both place values to be defensible.
    if not venue:
        touring_city = city_from_text(title)
        if touring_city and touring_city != DEFAULT_CITY:
            return None
        venue, city = DEFAULT_VENUE, DEFAULT_CITY

    if not city and DEFAULT_VENUE.casefold() in venue.casefold():
        city = DEFAULT_CITY

    details = item.get('start_date_details') or {}
    time_from = None
    if not item.get('all_day'):
        try:
            hour = int(details.get('hour'))
            minute = int(details.get('minutes'))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                time_from = f'{hour:02d}:{minute:02d}'
        except (TypeError, ValueError):
            pass

    if not title or not url or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'PL',
        'description': clean_text(item.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class FilharmoniaKoszalinskaPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filharmoniakoszalinska_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        records = []
        page = 1
        session = requests.Session()
        session.headers.update(HEADERS)
        while True:
            response = session.get(
                API_URL,
                params={
                    'per_page': 50,
                    'page': page,
                    'start_date': '2000-01-01 00:00:00',
                    'end_date': '2100-12-31 23:59:59',
                    'status': 'publish',
                },
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
            for item in payload.get('events', []):
                record = parse_event(item)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Filharmonia Koszalinska event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=item.get('url', ''),
                        error_type='IncompleteEventData',
                        error_message='Required title, date, venue, city, or URL is missing',
                    )
            if page >= int(payload.get('total_pages', 1)):
                break
            page += 1
        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['url']),
        )


def main():
    FilharmoniaKoszalinskaPlCrawler().run()


if __name__ == '__main__':
    main()
