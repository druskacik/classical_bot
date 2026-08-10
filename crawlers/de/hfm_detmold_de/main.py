import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hfm-detmold.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'konzerte/konzertkalender/')
SOURCE = 'Hochschule für Musik Detmold'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_location(raw_venue):
    venue = clean_text(raw_venue)
    if not venue:
        return None, None

    # Addresses occasionally appear in the location label. They are useful for
    # identifying the city, but are not part of the venue name stored downstream.
    if venue.startswith('S.A.A.L.'):
        return 'Detmold', 'S.A.A.L. (8.331), KreativInstitut.OWL'
    if venue.startswith('Kuppelsaal'):
        return 'Detmold', 'Kuppelsaal'

    outside_venues = {
        'Theater Gütersloh': 'Gütersloh',
        'Bartholomäuskirche Bielefeld-Brackwede': 'Bielefeld',
        'Kirche zu Bergkirchen': 'Bad Salzuflen',
    }
    if venue in outside_venues:
        return outside_venues[venue], venue

    # Unqualified rooms and churches in this institution's own calendar are in
    # Detmold. Performances outside the city are explicitly named above.
    return 'Detmold', venue


def description_from_entry(entry):
    parts = []
    title = entry.find('h2')
    for child in entry.children:
        if not getattr(child, 'name', None) or child is title:
            continue
        classes = set(child.get('class') or [])
        if classes & {
            'calendar-entry-date', 'calendar-entry-price',
            'calendar-entry-linkbooking', 'calendar-entry-more',
            'calendar-entry-close',
        }:
            continue
        if 'calendar-entry-detail' in classes:
            nodes = child.select('.calendar-entry-detail-text')
            values = [clean_text(node) for node in nodes]
        else:
            values = [clean_text(child)]
        for value in values:
            if value and value not in parts:
                parts.append(value)
    return '\n\n'.join(parts) or None


def parse_entry(entry):
    event_id = clean_text(entry.get('id'))
    title = clean_text(entry.find('h2'))
    date_line = clean_text(entry.select_one('.calendar-entry-date strong'))
    match = re.search(
        r'(\d{2}\.\d{2}\.\d{4})\s*\|\s*(.+?),\s*'
        r'(\d{1,2}:\d{2})\s*Uhr\b',
        date_line,
    )
    if not event_id or not title or not match:
        return None

    try:
        date = datetime.strptime(match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None
    city, venue = parse_location(match.group(2))
    if not city or not venue:
        return None

    return {
        'title': title,
        'date': date,
        'url': f'{CALENDAR_URL}#{event_id}',
        'time_from': match.group(3),
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': description_from_entry(entry),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    response = requests.get(CALENDAR_URL, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    records = []
    for entry in soup.select('.calendar-entry'):
        record = parse_entry(entry)
        if record:
            records.append(record)
        else:
            log_message(
                'Skipped invalid HfM Detmold calendar entry',
                event='crawler_item_skipped',
                level='warning',
                url=f'{CALENDAR_URL}#{entry.get("id", "")}',
            )
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['city'], item['title'], item['url']
    ))


class HfmDetmoldDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hfm_detmold_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    HfmDetmoldDeCrawler().run()


if __name__ == '__main__':
    main()
