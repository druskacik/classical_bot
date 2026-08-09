import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sinfonieorchesterbasel.ch/de/'
API_URL = urljoin(SOURCE_URL, 'api/events/')
SOURCE = 'Sinfonieorchester Basel'

HEADERS = {
    'Accept': 'application/json',
    'Accept-Language': 'de-CH,de;q=0.9,en;q=0.7',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

# The API has no country field. Swiss postal codes provide the normal case;
# these mappings keep explicitly listed tour venues from inheriting Switzerland.
COUNTRY_BY_CITY = {
    'Berlin': 'DE',
    'Freiburg im Breisgau': 'DE',
    'Hamburg': 'DE',
    'Luzern': 'CH',
    'München': 'DE',
    'Paris': 'FR',
    'Prag': 'CZ',
    'Ljubljana': 'SI',
    'Salzburg': 'AT',
    'Vienna': 'AT',
    'Wien': 'AT',
    'Zürich': 'CH',
}

CITY_BY_VENUE = {
    'Cankarjev Dom, Ljubljana': 'Ljubljana',
    'Grosses Festspielhaus': 'Salzburg',
    'Messe, Halle Nord': 'Basel',
    'Musikverein Wien': 'Wien',
    'Prinzregententheater, München': 'München',
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(html.unescape(str(value)), 'html.parser')
    text = soup.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_event_list(session):
    events = []
    url = API_URL
    params = {'date': '2000-01-01'}
    while url:
        response = session.get(url, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        events.extend(payload.get('results') or [])
        url = payload.get('next')
        params = None
    return events


def fetch_detail(event_id):
    response = requests.get(
        f'{API_URL}{event_id}/', headers=HEADERS, timeout=60
    )
    response.raise_for_status()
    return response.json()


def extract_city(venue):
    city = clean_text(venue.get('city'))
    if city:
        return city

    address = clean_text(venue.get('address'))
    match = re.search(r'(?m)(?:CH-)?\d{4}\s+([^\n,]+)$', address)
    if match:
        return match.group(1).strip()

    venue_name = clean_text(venue.get('name'))
    if venue_name in CITY_BY_VENUE:
        return CITY_BY_VENUE[venue_name]
    if 'Basel' in venue_name or 'Kleinhüningen' in venue_name:
        return 'Basel'
    return ''


def extract_country_code(venue, city):
    mapped = COUNTRY_BY_CITY.get(city)
    if mapped:
        return mapped

    address = clean_text(venue.get('address'))
    lowered = address.lower()
    country_names = {
        'deutschland': 'DE', 'germany': 'DE', 'france': 'FR',
        'frankreich': 'FR', 'österreich': 'AT', 'austria': 'AT',
        'italien': 'IT', 'italy': 'IT', 'schweiz': 'CH',
        'switzerland': 'CH',
    }
    for country, code in country_names.items():
        if country in lowered:
            return code

    postal_code = clean_text(venue.get('postal_code'))
    if re.fullmatch(r'\d{4}', postal_code) or re.search(
        r'(?m)(?:CH-)?\d{4}\s+[^\n,]+$', address
    ):
        return 'CH'
    if city == 'Basel':
        return 'CH'
    return None


def format_work(work):
    composer = clean_text(work.get('description_short'))
    name = clean_text(work.get('name'))
    description = clean_text(
        work.get('description_long') or work.get('description')
    )
    movements = [
        clean_text(movement.get('name') or movement.get('description'))
        for movement in (work.get('movements') or [])
    ]
    parts = [part for part in (composer, name, description, *movements) if part]
    return ' — '.join(parts)


def build_description(event):
    sections = []
    for field in (
        'headline_description', 'description_short', 'description_short2',
        'description_long', 'item_description', 'works_manual',
    ):
        text = clean_text(event.get(field))
        if text and text not in sections:
            sections.append(text)

    works = [format_work(work) for work in (event.get('works') or [])]
    works = [work for work in works if work and work != '- Pause -']
    if works:
        sections.append('Programm\n' + '\n'.join(works))
    return '\n\n'.join(sections) or None


def parse_event(summary, detail):
    title = clean_text(detail.get('name') or summary.get('name'))
    start = detail.get('date_start') or summary.get('date_start') or ''
    venue_data = detail.get('venue') or summary.get('venue') or {}
    venue = clean_text(
        venue_data.get('name_detail_manual') or venue_data.get('name')
    )
    city = extract_city(venue_data)
    country_code = extract_country_code(venue_data, city)
    slug = (summary.get('slug') or detail.get('slug') or {}).get('de')
    event_id = summary.get('id') or detail.get('id')
    if not all((title, start, venue, city, country_code, slug, event_id)):
        return None

    try:
        start_at = datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None

    url = urljoin(SOURCE_URL, f'konzerte/{slug}/{event_id}')
    return {
        'title': title,
        'date': start_at.date().isoformat(),
        'url': url,
        'time_from': start_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': build_description(detail),
    }


class SinfonieorchesterBaselChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sinfonieorchesterbasel_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            summaries = fetch_event_list(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Sinfonieorchester Basel event list',
                event='crawler_fetch_failed', level='error', url=API_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        details = {}
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(fetch_detail, item.get('id')): item
                for item in summaries if item.get('id')
            }
            for future in as_completed(futures):
                summary = futures[future]
                try:
                    details[summary['id']] = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to fetch Sinfonieorchester Basel event detail',
                        event='crawler_item_fetch_failed', level='warning',
                        url=f'{API_URL}{summary["id"]}/',
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        for summary in summaries:
            detail = details.get(summary.get('id'))
            record = parse_event(summary, detail) if detail else None
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped Sinfonieorchester Basel event with incomplete data',
                    event='crawler_item_skipped', level='warning',
                    url=f'{API_URL}{summary.get("id")}/',
                )
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    SinfonieorchesterBaselChCrawler().run()


if __name__ == '__main__':
    main()
