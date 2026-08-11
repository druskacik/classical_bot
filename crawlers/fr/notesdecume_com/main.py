import html
import re
import unicodedata
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.notesdecume.com/'
SOURCE = "Festival Notes d'Ecume"
API_URL = f'{SOURCE_URL}wp-json/wp/v2'
CONCERT_CATEGORY_SLUG = 'concert'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janvier': 1,
    'fevrier': 2,
    'mars': 3,
    'avril': 4,
    'mai': 5,
    'juin': 6,
    'juillet': 7,
    'aout': 8,
    'septembre': 9,
    'octobre': 10,
    'novembre': 11,
    'decembre': 12,
}

VENUE_CITIES = {
    'espace henry de monfreid': 'Port Leucate',
    "eglise notre-dame de l'assomption": 'Leucate Village',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized(value):
    return ''.join(
        character for character in unicodedata.normalize('NFKD', clean_text(value).lower())
        if not unicodedata.combining(character)
    ).replace('’', "'")


def parse_date_and_time(value):
    match = re.search(
        r'\b(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s+'
        r'(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})\b(?:\s+(\d{1,2})\s*h(?:\s*(\d{2}))?)?',
        clean_text(value),
        flags=re.IGNORECASE,
    )
    if not match:
        return None, None
    month = MONTHS.get(normalized(match.group(2)))
    if not month:
        return None, None
    try:
        event_date = date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None, None
    time_from = None
    if match.group(4):
        hour = int(match.group(4))
        minute = int(match.group(5) or 0)
        if hour < 24 and minute < 60:
            time_from = f'{hour:02d}:{minute:02d}'
    return event_date, time_from


def parse_location(value):
    text = clean_text(value)
    text = re.sub(
        r'^.*?20\d{2}\b(?:\s+\d{1,2}\s*h(?:\s*\d{2})?)?', '', text,
        count=1, flags=re.DOTALL,
    ).strip(' \n-|')
    city_match = re.search(r'[\[(]\s*(Port Leucate|Leucate Village|Leucate)\s*[\])]', text, re.I)
    city = clean_text(city_match.group(1)) if city_match else ''
    venue = re.sub(r'\s*[\[(]\s*(?:Port Leucate|Leucate Village|Leucate)\s*[\])]\s*$', '', text, flags=re.I)

    suffix_match = re.search(r',\s*(Port Leucate|Leucate Village|Leucate)\s*$', venue, re.I)
    if suffix_match:
        city = clean_text(suffix_match.group(1))
        venue = venue[:suffix_match.start()].strip()

    venue_key = normalized(venue)
    if not city:
        city = next(
            (known_city for known_venue, known_city in VENUE_CITIES.items() if known_venue in venue_key),
            '',
        )
    return venue.strip(), city


def extract_description(soup):
    parts = []
    widgets = soup.select('.elementor-widget-text-editor')
    for widget in widgets[1:]:
        text = clean_text(widget)
        key = normalized(text)
        if not text or key.startswith('liens utiles') or key.startswith('vos billets'):
            continue
        if text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_post(post):
    title = clean_text(BeautifulSoup(post.get('title', {}).get('rendered', ''), 'html.parser'))
    url = clean_text(post.get('link'))
    soup = BeautifulSoup(post.get('content', {}).get('rendered', ''), 'html.parser')
    metadata = soup.select_one('.elementor-widget-text-editor')
    metadata_text = clean_text(metadata)
    event_date, time_from = parse_date_and_time(metadata_text)
    venue, city = parse_location(metadata_text)
    if not title or not event_date or not url or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'FR',
        'description': extract_description(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class NotesDecumeComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='notesdecume_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        categories_response = session.get(
            f'{API_URL}/categories', params={'slug': CONCERT_CATEGORY_SLUG}, timeout=45,
        )
        categories_response.raise_for_status()
        categories = categories_response.json()
        if not categories:
            raise RuntimeError(f'WordPress category not found: {CONCERT_CATEGORY_SLUG}')

        records = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            response = session.get(
                f'{API_URL}/posts',
                params={
                    'categories': categories[0]['id'],
                    'per_page': 100,
                    'page': page,
                    'orderby': 'date',
                    'order': 'desc',
                },
                timeout=45,
            )
            response.raise_for_status()
            total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            for post in response.json():
                record = parse_post(post)
                if record:
                    records.append(record)
                else:
                    log_message(
                        "Skipped incomplete Notes d'Ecume concert",
                        event='crawler_item_skipped',
                        level='warning',
                        url=clean_text(post.get('link')) or SOURCE_URL,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, URL, venue, or city is missing',
                    )
            page += 1

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    NotesDecumeComCrawler().run()


if __name__ == '__main__':
    main()
