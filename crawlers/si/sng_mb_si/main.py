import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sng-mb.si/'
CALENDAR_URL = f'{SOURCE_URL}opera-program/'
SOURCE = 'SNG Maribor'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'sl-SI,sl;q=0.9,en;q=0.7',
}

# Touring locations are free text.  Only explicit place names are used; an
# unknown touring venue is skipped rather than being assigned to Maribor.
FOREIGN_PLACES = {
    'bonn': ('Bonn', 'DE'),
    'fürstenfeldbruck': ('Fürstenfeldbruck', 'DE'),
    'ludwigsburg': ('Ludwigsburg', 'DE'),
    'leipzig': ('Leipzig', 'DE'),
    'ludwigshafen': ('Ludwigshafen am Rhein', 'DE'),
    'reka': ('Rijeka', 'HR'),
    'zagreb': ('Zagreb', 'HR'),
    'dubrovnik': ('Dubrovnik', 'HR'),
    'budimpešta': ('Budapest', 'HU'),
    'praga': ('Prague', 'CZ'),
    'modena': ('Modena', 'IT'),
    'pordenone': ('Pordenone', 'IT'),
    'trst': ('Trieste', 'IT'),
    'monfalcone': ('Monfalcone', 'IT'),
    'cremona': ('Cremona', 'IT'),
    'parma': ('Parma', 'IT'),
    'cagliari': ('Cagliari', 'IT'),
    'bologna': ('Bologna', 'IT'),
    'atene': ('Athens', 'GR'),
    'paphos': ('Paphos', 'CY'),
    'limassol': ('Limassol', 'CY'),
    'taškent': ('Tashkent', 'UZ'),
    'ženeva': ('Geneva', 'CH'),
    'zürich': ('Zürich', 'CH'),
    'bern': ('Bern', 'CH'),
    'dubaj': ('Dubai', 'AE'),
    'hong kong': ('Hong Kong', 'HK'),
    'lafnitz': ('Lafnitz', 'AT'),
}

SLOVENIAN_PLACES = {
    'maribor': 'Maribor',
    'ptuj': 'Ptuj',
    'ljubljana': 'Ljubljana',
    'ljubjana': 'Ljubljana',
    'bled': 'Bled',
    'blejski otok': 'Bled',
    'murska sobota': 'Murska Sobota',
    'lendava': 'Lendava',
    'velenje': 'Velenje',
    'portorož': 'Portorož',
    'radlje ob dravi': 'Radlje ob Dravi',
    'rače': 'Rače',
}

HOME_VENUES = (
    'velika dvorana',
    'dvorana frana žižka',
    'dvorana ondine otta klasinc',
    'dvorana ondine otte klasinc',
    'dvorana union',
    'unionska dvorana',
    'kazinska dvorana',
    'grajska ploščad v mestnem parku',
    'velika dorana',
    'stolna cerkev maribor',
    'promenada mestnega parka maribor',
    'glavni trg maribor',
    'glavni oder festivala lent',
    'sng maribor',
)


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    else:
        value = str(value)
        if '<' in value and '>' in value:
            value = BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
    value = html.unescape(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def parse_time(value):
    match = re.fullmatch(r'([01]?\d|2[0-3])[.:]([0-5]\d)', clean_text(value))
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_location(value):
    venue = clean_text(value).strip(' ,')
    lower = venue.casefold()
    if not venue or re.search(r'odpade|\bura\b', lower):
        return None

    for token, (city, country_code) in FOREIGN_PLACES.items():
        if token in lower:
            return venue, city, country_code
    if 'vatroslav lisinski' in lower:
        return venue, 'Zagreb', 'HR'
    if 'cankarjev dom' in lower or 'cankarjevega doma' in lower:
        return venue, 'Ljubljana', 'SI'
    for token, city in SLOVENIAN_PLACES.items():
        if token in lower:
            return venue, city, 'SI'
    if any(lower == item or lower.startswith(f'{item},') for item in HOME_VENUES):
        return venue, 'Maribor', 'SI'
    return None


def parse_calendar(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    calendar = soup.select_one('#calendarAllList[data-events]')
    if calendar is None:
        raise ValueError('Opera/Ballet calendar data was not found')
    try:
        events = json.loads(calendar['data-events'])
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError('Opera/Ballet calendar data is invalid JSON') from error
    if not isinstance(events, list):
        raise ValueError('Opera/Ballet calendar data is not a list')
    return events


def make_record(event, description=None):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('event_link'))
    event_date = clean_text(event.get('start'))
    location = parse_location(event.get('location') or event.get('place'))
    try:
        event_date = date.fromisoformat(event_date).isoformat()
    except ValueError:
        return None
    if not title or not url.startswith(SOURCE_URL) or not location:
        return None

    venue, city, country_code = location
    body = clean_text(description)
    author = clean_text(event.get('author'))
    if author and author.casefold() not in body.casefold():
        body = f'{author}\n\n{body}'.strip()
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(event.get('time')),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': body or None,
    }


class SngMbSiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sng_mb_si',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='SI',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def _description(self, session, url):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            return clean_text(soup.select_one('.opis__desc--txt')) or None
        except requests.RequestException as error:
            log_message(
                'Failed to fetch SNG Maribor event detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return None

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(CALENDAR_URL, timeout=60)
            response.raise_for_status()
            events = parse_calendar(response.content)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch SNG Maribor Opera/Ballet calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        urls = {
            clean_text(item.get('event_link'))
            for item in events
            if clean_text(item.get('event_link')).startswith(SOURCE_URL)
        }
        descriptions = {}
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {
                executor.submit(self._description, session, url): url
                for url in urls
            }
            for future in as_completed(futures):
                descriptions[futures[future]] = future.result()

        records = []
        for event in events:
            url = clean_text(event.get('event_link'))
            record = make_record(event, descriptions.get(url))
            if record:
                records.append(record)
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    SngMbSiCrawler().run()


if __name__ == '__main__':
    main()
