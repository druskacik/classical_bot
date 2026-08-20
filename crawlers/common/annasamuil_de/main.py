import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://annasamuil.de/'
# The canonical HTTPS endpoint currently fails TLS negotiation, while the
# site's own HTTP endpoint serves the schedule successfully.
SCHEDULE_URL = 'http://annasamuil.de/schedule.php'
SOURCE = 'Anna Samuil'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9,de;q=0.8',
}

# The calendar does not provide dedicated location fields. These mappings use
# the first-party venue image plus the location text printed on each card.
LOCATION_BY_IMAGE = {
    'staatsoperberlin.jpg': ('Staatsoper Unter den Linden', 'Berlin', 'DE'),
    'victoria.jpg': ('Victoria Music Center', 'Barcelona', 'ES'),
    'schlossacademy.jpg': ('Künstlerhof Alt-Lietzow', 'Berlin', 'DE'),
    'shanghai.jpg': ('Yibo Music Center', 'Shanghai', 'CN'),
    'bejing.jpg': ('China Conservatory of Music', 'Beijing', 'CN'),
    'triomphe.jpg': ('Salon Piano Maene', 'Brussels', 'BE'),
    'veraoclassico.jpg': ('Centro Cultural de Belém', 'Lisbon', 'PT'),
}

MONTHS = {
    month.upper(): number
    for number, month in enumerate(
        ('', 'January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December')
    )
    if month
}


def clean_text(node):
    if node is None:
        return ''
    value = node.get_text(' ', strip=True) if hasattr(node, 'get_text') else str(node)
    return re.sub(r'\s+', ' ', value.replace('\xa0', ' ')).strip()


def parse_season(html):
    match = re.search(r'SEASON\s+(\d{4})\D{1,5}(\d{4})', html, re.I)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def parse_occurrence(sheet, season):
    month_name = clean_text(sheet.select_one('.month')).upper()
    day_text = clean_text(sheet.select_one('.day'))
    time_from = clean_text(sheet.select_one('.time')) or None
    if month_name not in MONTHS or not day_text.isdigit():
        return None
    month = MONTHS[month_name]
    year = season[0] if month >= 8 else season[1]
    try:
        date = datetime(year, month, int(day_text)).date().isoformat()
    except ValueError:
        return None
    if time_from and not re.fullmatch(r'(?:[01]?\d|2[0-3]):[0-5]\d', time_from):
        time_from = None
    elif time_from:
        hour, minute = time_from.split(':')
        time_from = f'{int(hour):02d}:{minute}'
    return date, time_from


def parse_schedule(html):
    soup = BeautifulSoup(html, 'html.parser')
    season = parse_season(clean_text(soup))
    if season is None:
        return []

    records = []
    for card in soup.select('table.calendar-row'):
        image = card.select_one('.calendar-pic img[src]')
        image_name = image['src'].rsplit('/', 1)[-1].lower() if image else ''
        location = LOCATION_BY_IMAGE.get(image_name)
        if location is None:
            # A city or festival name alone is not a defensible venue.
            continue
        venue, city, country_code = location

        composer = clean_text(card.select_one('.composer'))
        work = clean_text(card.select_one('.opera'))
        role = clean_text(card.select_one('.role'))
        details = clean_text(card.select_one('.conductor'))
        title = work or composer
        description = ' — '.join(part for part in (composer, work, role, details) if part) or None
        if not title:
            continue

        for sheet in card.select('.calendar-sheet'):
            occurrence = parse_occurrence(sheet, season)
            if occurrence is None:
                continue
            date, time_from = occurrence
            records.append({
                'title': title,
                'date': date,
                'url': SCHEDULE_URL,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class AnnaSamuilDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='annasamuil_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(SCHEDULE_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
            response.encoding = 'utf-8'
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Anna Samuil schedule',
                event='crawler_fetch_failed', level='error', url=SCHEDULE_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            return []
        records = parse_schedule(response.text)
        records.sort(key=lambda row: (row['date'], row['time_from'] or '', row['title']))
        return records


def main():
    AnnaSamuilDeCrawler().run()


if __name__ == '__main__':
    main()
