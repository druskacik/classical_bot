import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sdclaspezia.it/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/'
SOURCE = 'Società dei Concerti La Spezia'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}

PROVINCE_CITIES = {
    'ameglia', 'arcola', 'beverino', 'bolano', 'bonassola', 'borghetto di vara',
    'brugnato', 'calice al cornoviglio', 'carro', 'carrodano', 'castelnuovo magra',
    'deiva marina', 'follo', 'framura', 'la spezia', 'lerici', 'levanto',
    'maissana', 'monterosso al mare', 'pignone', 'portovenere', 'riccò del golfo',
    'riomaggiore', 'rocchetta di vara', 'santo stefano di magra', 'sarzana',
    'sesta godano', 'varese ligure', 'vernazza', 'vezzano ligure', 'zignago',
}

CITY_ALIASES = {
    'borghetto vara': 'Borghetto di Vara',
    'carro': 'Carro',
    'montemarcello': 'Ameglia',
    'ponzano magara': 'Santo Stefano di Magra',
    'ponzano magra': 'Santo Stefano di Magra',
    'ponzano superiore': 'Santo Stefano di Magra',
    's. stefano di magra': 'Santo Stefano di Magra',
    's. stefano magra': 'Santo Stefano di Magra',
    'santo stefano': 'Santo Stefano di Magra',
    'santo stefano magra': 'Santo Stefano di Magra',
    'stefano magra': 'Santo Stefano di Magra',
    'tivegna': 'Follo',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def shortcode_block(content, label):
    decoded = html.unescape(content)
    pattern = (
        rf'\[vc_custom_heading\s+[^\]]*text=["“”]{label}["“”][^\]]*\]'
        rf'.*?\[vc_column_text[^\]]*\](.*?)\[/vc_column_text\]'
    )
    match = re.search(pattern, decoded, re.I | re.S)
    if not match:
        return ''
    return clean_text(BeautifulSoup(match.group(1), 'html.parser'))


def description_from_content(content):
    decoded = html.unescape(content)
    decoded = re.sub(r'\[/?vc_[^\]]*\]', '\n', decoded, flags=re.I)
    decoded = re.sub(r'\[/?lab_[^\]]*\]', '\n', decoded, flags=re.I)
    return clean_text(BeautifulSoup(decoded, 'html.parser')) or None


def event_date(value, published):
    if re.search(r'\bdal\s+\d{1,2}\s+al\s+\d{1,2}\b', value, re.I):
        return None
    match = re.search(
        r'\b(?:luned[iì]|marted[iì]|mercoled[iì]|gioved[iì]|venerd[iì]|sabato|domenica)?'
        r'\s*(\d{1,2})\s*(gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|'
        r'settembre|ottobre|novembre|dicembre)(?:\s+(20\d{2}))?\b',
        value, re.I,
    )
    if not match:
        return None
    day = int(match.group(1))
    month = MONTHS[match.group(2).casefold()]
    published_date = date.fromisoformat(published[:10])
    year = int(match.group(3)) if match.group(3) else published_date.year
    # Events are commonly posted shortly before their performance. This also
    # handles December announcements for the following January.
    if not match.group(3) and month < published_date.month - 6:
        year += 1
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def event_time(value):
    match = re.search(r'\b(?:ore\s*)?(\d{1,2})[.:](\d{2})\b', value, re.I)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        return None
    return f'{int(match.group(1)):02d}:{int(match.group(2)):02d}'


def location(value):
    text = re.sub(r'^(?:dove|presso)\s*[:\-]?\s*', '', clean_text(value), flags=re.I)
    text = re.sub(r'[ \t]+', ' ', text).strip(' ,;-\n')
    if not text:
        return None

    parts = [part.strip() for part in re.split(r'[,|\n]', text) if part.strip()]
    first = parts[0].casefold() if parts else ''
    if len(parts) >= 2 and first in CITY_ALIASES:
        return ', '.join(parts[1:]), CITY_ALIASES[first]
    if len(parts) >= 2 and first in PROVINCE_CITIES:
        return ', '.join(parts[1:]), parts[0]
    if len(parts) >= 2 and parts[-1].casefold() in PROVINCE_CITIES:
        return ', '.join(parts[:-1]), parts[-1]

    match = re.match(r'([^,]+?)\s+(?:loc\.?|localit[aà])\s+(.+)', text, re.I)
    if match and match.group(1).strip().casefold() in PROVINCE_CITIES:
        return f'Località {match.group(2).strip()}', match.group(1).strip()

    folded = text.casefold()
    for alias in sorted(CITY_ALIASES, key=len, reverse=True):
        if re.search(rf'\b{re.escape(alias)}\b', folded):
            venue = re.sub(rf'\b{re.escape(alias)}\b', '', text, flags=re.I).strip(' ,;-–\n')
            if venue:
                return venue, CITY_ALIASES[alias]
    for city in sorted(PROVINCE_CITIES, key=len, reverse=True):
        if re.search(rf'\b{re.escape(city)}\b', folded):
            venue = re.sub(rf'\b{re.escape(city)}\b', '', text, flags=re.I).strip(' ,;-')
            if venue:
                return venue, city.title()

    # The remaining single-name locations in this venue calendar are La Spezia
    # halls; a city is inferred, while the published hall name remains the venue.
    if re.search(
        r'\b(teatro|chiesa|museo|conservatorio|auditorium|oratorio|palazzina|'
        r'castello san giorgio|piazza brin|palazzo della provincia|sala dante|'
        r'liceo musicale|santuario)\b',
        text, re.I,
    ):
        return text, 'La Spezia'
    external = re.match(r'(.+?)(?:\s*\([A-Z]{2}\))?\s*[–-]\s*(.+)', text)
    if external and external.group(1).strip() and external.group(2).strip():
        return external.group(2).strip(), external.group(1).strip()
    return None


def parse_item(item):
    content = item.get('content', {}).get('rendered', '')
    when = shortcode_block(content, 'Quando')
    where = shortcode_block(content, 'Dove')
    hour = shortcode_block(content, 'Ore')
    if re.search(r'\b(?:youtube|streaming|diretta\s+(?:web|online))\b', where, re.I):
        return None
    parsed_location = location(where)
    parsed_date = event_date(when, item['date'])
    title = clean_text(BeautifulSoup(item.get('title', {}).get('rendered', ''), 'html.parser'))
    url = item.get('link', '').strip()
    if not all((title, parsed_date, url, parsed_location)):
        return None
    venue, city = parsed_location
    return {
        'title': title,
        'date': parsed_date,
        'url': url,
        'time_from': event_time(hour),
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': description_from_content(content),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class SdclaspeziaItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sdclaspezia_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for endpoint in ('portfolio', 'mec-events'):
            page = 1
            while True:
                url = f'{API_URL}{endpoint}'
                try:
                    response = session.get(
                        url,
                        params={'per_page': 100, 'page': page},
                        timeout=45,
                    )
                    response.raise_for_status()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Società dei Concerti events API',
                        event='crawler_fetch_failed',
                        level='error',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    raise

                for item in response.json():
                    record = parse_item(item)
                    if record:
                        records.append(record)

                total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
                if page >= total_pages:
                    break
                page += 1

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    SdclaspeziaItCrawler().run()


if __name__ == '__main__':
    main()
