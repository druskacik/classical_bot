import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.fliessenfestival.de/'
CONCERTS_URL = f'{SOURCE_URL}konzerte/'
SOURCE = 'Internationales Kammermusikfestival Fliessen'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1,
    'februar': 2,
    'märz': 3,
    'maerz': 3,
    'april': 4,
    'mai': 5,
    'juni': 6,
    'juli': 7,
    'august': 8,
    'september': 9,
    'oktober': 10,
    'november': 11,
    'dezember': 12,
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value):
    match = re.search(
        r'(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\s+(20\d{2})\s*\|\s*'
        r'(\d{1,2}):(\d{2})\s*Uhr',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None, None
    month = MONTHS.get(match.group(2).lower())
    if month is None:
        return None, None
    try:
        event_date = date(
            int(match.group(3)), month, int(match.group(1))
        ).isoformat()
    except ValueError:
        return None, None
    event_time = f'{int(match.group(4)):02d}:{match.group(5)}'
    return event_date, event_time


def resolve_city(location, venue):
    value = f'{location} {venue}'.lower()
    if 'baruth' in value or 'glashütte' in value:
        return 'Baruth/Mark'
    if 'bornsdorf' in value or 'drauschemühle' in value:
        return 'Luckau'
    if 'luckau' in value:
        return 'Luckau'
    if 'lübbenau' in value:
        return 'Lübbenau/Spreewald'
    return None


def programme_text(section):
    for title in section.select('.qodef-e-title'):
        if clean_text(title).lower() != 'programm':
            continue
        heading = title.find_parent(['h3', 'h4', 'h5', 'h6'])
        content = heading.find_next_sibling(class_='qodef-e-content') if heading else None
        description = clean_text(content)
        return description or None
    return None


def get_concerts():
    response = requests.get(CONCERTS_URL, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    records = []
    for section in soup.select('section.elementor-top-section'):
        section_text = clean_text(section)
        event_date, event_time = parse_datetime(section_text)
        if not event_date:
            continue

        location_heading = section.select_one('h4.qodef-m-title')
        location = clean_text(location_heading)
        venue_element = (
            location_heading.find_next('p', class_='qodef-m-text')
            if location_heading else None
        )
        venue = clean_text(venue_element)
        city = resolve_city(location, venue)
        if not location or not venue or not city:
            log_message(
                'Skipping Fliessen concert with incomplete location data',
                event='crawler_item_skipped',
                level='warning',
                url=CONCERTS_URL,
                date=event_date,
                location=location or None,
                venue=venue or None,
            )
            continue

        records.append({
            'title': f'Fliessen Festival – {location}',
            'date': event_date,
            'url': CONCERTS_URL,
            'time_from': event_time,
            'venue': venue,
            'city': city,
            'country_code': 'DE',
            'description': programme_text(section),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class FliessenfestivalDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fliessenfestival_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    FliessenfestivalDeCrawler().run()


if __name__ == '__main__':
    main()
