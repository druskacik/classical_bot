import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://garyryan.co.uk/'
SOURCE = 'Gary Ryan'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
HEADERS = {
    'Accept': 'application/json',
    'User-Agent': 'Mozilla/5.0 (compatible; ClassicalBot/1.0)',
}

# The site is British but publishes Gary Ryan's touring engagements.  These
# markers prevent an overseas performance from inheriting the home country.
COUNTRY_MARKERS = {
    'Australia': 'AU',
    'Belgium': 'BE',
    'Brussels': 'BE',
    'China': 'CN',
    'Copenhagen': 'DK',
    'Denmark': 'DK',
    'Dublin': 'IE',
    'France': 'FR',
    'Germany': 'DE',
    'Gevelsberg': 'DE',
    'Holland': 'NL',
    'India': 'IN',
    'Ireland': 'IE',
    'Malaysia': 'MY',
    'New Zealand': 'NZ',
    'Portugal': 'PT',
    'Ronda': 'ES',
    'Singapore': 'SG',
    'Spain': 'ES',
    'Sweden': 'SE',
}

# The Events Calendar data is old and most entries predate its structured venue
# fields.  Use only explicit place names present in a title or description.
CITY_MARKERS = [
    'Aldeburgh', 'Almere', 'Audincourt', 'Bexleyheath', 'Bognor Regis',
    'Bradford-on-Avon', 'Braga', 'Bridport', 'Bridgwater', 'Bromley',
    'Brussels', 'Bury St. Edmunds', 'Caernarfon', 'Cardiff', 'Chichester',
    'Cirencester', 'Colchester', 'Copenhagen', 'Donegal', 'Dublin',
    'Edinburgh', 'Edgecliff', 'Frant', 'Gevelsberg', 'Gillingham',
    'Glastonbury', 'Glasgow', 'Grassington', 'Guildford', 'Hastings',
    'Hitchin', 'Hornchurch', 'Ingesund', 'Keele', 'Kings Lynn', 'London',
    'Lymington', 'Manchester', 'Minehead', 'Mylor', 'Petworth', 'Pentyrch',
    'Reading', 'Rotenburg', 'Ronda', 'Salisbury', 'Sevenoaks', 'Southampton',
    'Southend on Sea', 'Stansted Mountfitchet', 'Sturminster Newton',
    'Swansea', 'Swindon', 'Sydney', 'Tonbridge', 'Torquay', 'Ullapool',
    'Weston', 'Winchester',
]


def clean_text(value):
    if not value:
        return ''
    raw = html.unescape(str(value))
    text = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True) if '<' in raw else raw
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def country_code_for(event, venue, text):
    country = clean_text(venue.get('country')).casefold()
    country_names = {
        'united kingdom': 'GB', 'uk': 'GB', 'australia': 'AU', 'belgium': 'BE',
        'china': 'CN', 'denmark': 'DK', 'france': 'FR', 'germany': 'DE',
        'india': 'IN', 'ireland': 'IE', 'republic of ireland': 'IE',
        'malaysia': 'MY', 'netherlands': 'NL', 'new zealand': 'NZ',
        'portugal': 'PT', 'singapore': 'SG', 'spain': 'ES', 'sweden': 'SE',
    }
    if country in country_names:
        return country_names[country]
    for marker, code in COUNTRY_MARKERS.items():
        if re.search(rf'\b{re.escape(marker)}\b', text, re.I):
            return code
    return 'GB'


def city_for(venue, text):
    city = clean_text(venue.get('city'))
    if city:
        return city
    for marker in sorted(CITY_MARKERS, key=len, reverse=True):
        if re.search(rf'\b{re.escape(marker)}\b', text, re.I):
            return marker
    return ''


def venue_for(venue, title, city):
    name = clean_text(venue.get('venue'))
    if name and name.casefold() != city.casefold():
        return name

    candidate = re.sub(
        r'^(?:evening |lunchtime )?recital\s*[–-]\s*', '', title, flags=re.I
    )
    candidate = re.sub(r'\s*[–-]\s*(?:6 Hands|\d{1,2}.*)$', '', candidate, flags=re.I)
    candidate = re.sub(r'\s*\([^)]*\)\s*$', '', candidate).strip(' ,.–-')
    # Prefer a named building/organisation rather than an address or bare city.
    venue_words = (
        r'Academy|Arts Centre|Centre|Chapel|Church|Club|College|Conservator|Festival|'
        r'Gallery|Granary|Hall|House|Music Society|Music Club|Playhouse|School|'
        r'Theatre|University'
    )
    matches = []
    for segment in re.split(r'\s*[–,]\s*', candidate):
        match = re.search(rf'(.+?\b(?:{venue_words})\b)', segment, re.I)
        if match:
            matches.append(match.group(1).strip(' ,.–-'))
    if matches:
        # Later comma-separated components are normally the concrete building;
        # earlier ones tend to be the presenting festival, club, or society.
        value = matches[-1]
        if value.casefold() != city.casefold():
            return value
    return ''


def event_time(event, text):
    if not event.get('all_day'):
        try:
            return datetime.strptime(event['start_date'], '%Y-%m-%d %H:%M:%S').strftime('%H:%M')
        except (KeyError, TypeError, ValueError):
            pass
    match = re.search(r'\b(1[0-2]|0?[1-9])(?:[.:](\d{2}))?\s*(am|pm)\b', text, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == 'pm' else 0)
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def parse_event(event):
    title = clean_text(event.get('title'))
    description = clean_text(event.get('description'))
    url = clean_text(event.get('url'))
    venue_data = event.get('venue') if isinstance(event.get('venue'), dict) else {}
    evidence = '\n'.join((title, description))
    city = city_for(venue_data, evidence)
    venue = venue_for(venue_data, title, city)
    try:
        date = datetime.strptime(event.get('start_date', ''), '%Y-%m-%d %H:%M:%S').date().isoformat()
    except (TypeError, ValueError):
        date = ''
    if not all((title, date, url, venue, city)):
        return None
    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': event_time(event, evidence),
        'venue': venue,
        'city': city,
        'country_code': country_code_for(event, venue_data, evidence),
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class GaryRyanCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='garyryan_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        params = {
            'per_page': 50,
            'page': 1,
            'start_date': '2000-01-01',
            'end_date': '2100-12-31',
            'status': 'publish',
        }
        records = []
        while True:
            response = session.get(API_URL, params=params, headers=HEADERS, timeout=45)
            response.raise_for_status()
            payload = response.json()
            for event in payload.get('events') or []:
                record = parse_event(event)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped Gary Ryan event with incomplete location data',
                        event='crawler_item_skipped',
                        level='warning',
                        url=clean_text(event.get('url')),
                        error_type='IncompleteEventData',
                        error_message='Required title, date, URL, venue, or city is missing',
                    )
            total_pages = int(payload.get('total_pages') or 0)
            if params['page'] >= total_pages:
                break
            params['page'] += 1
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    GaryRyanCoUkCrawler().run()


if __name__ == '__main__':
    main()
