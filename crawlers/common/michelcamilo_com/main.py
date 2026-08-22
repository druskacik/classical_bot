import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.michelcamilo.com/'
TOUR_URL = urljoin(SOURCE_URL, 'tour')
TOUR_JSON_URL = f'{TOUR_URL}?format=json'
SOURCE = 'Michel Camilo'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2,
    'mar': 3, 'march': 3, 'apr': 4, 'april': 4,
    'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
    'aug': 8, 'august': 8, 'sep': 9, 'september': 9,
    'oct': 10, 'october': 10, 'nov': 11, 'november': 11,
    'dec': 12, 'december': 12,
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4,
    'mayo': 5, 'junio': 6, 'julio': 7, 'agosto': 8,
    'septiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u3000', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    match = re.search(r'(\d{1,2})(?::|\.)(\d{2})\s*([ap]m)?', value, re.I)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    marker = (match.group(3) or '').lower()
    if marker == 'pm' and hour < 12:
        hour += 12
    elif marker == 'am' and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def make_date(year, month, day):
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def extract_occurrences(paragraphs, default_year):
    occurrences = []
    for paragraph in paragraphs:
        # Tokyo-style schedules name several dates followed by two start times.
        numeric_dates = re.findall(r'(?<!\d)(\d{1,2})\.(\d{1,2})(?!\d)', paragraph)
        starts = re.findall(r'Start\s*(\d{1,2}:\d{2}\s*[ap]m)', paragraph, re.I)
        if numeric_dates and starts:
            year_match = re.search(r'\b(20\d{2})\b', paragraph)
            year = int(year_match.group(1)) if year_match else default_year
            for month, day in numeric_dates:
                event_date = make_date(year, month, day)
                for start in starts:
                    if event_date and parse_time(start):
                        occurrences.append((event_date, parse_time(start)))
            continue

        # Spanish pages use day de month order.
        month_pattern = '|'.join(sorted(MONTHS, key=len, reverse=True))
        spanish = re.search(
            r'\b(\d{1,2})\s+de\s+(' + month_pattern + r')\b'
            r'(?:\s+de\s+(20\d{2}))?', paragraph, re.I,
        )
        if spanish:
            match = None
            month_name, day, year = spanish.group(2), spanish.group(1), spanish.group(3)
        else:
            # English month-first dates, with or without an explicitly repeated year.
            match = re.search(
                r'\b(' + month_pattern + r')\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b'
                r'(?:,?\s*(20\d{2}))?', paragraph, re.I,
            )
            if not match:
                continue
            month_name, day, year = match.group(1), match.group(2), match.group(3)

        event_date = make_date(year or default_year, MONTHS[month_name.lower()], day)
        if not event_date:
            continue
        tail = paragraph[match.end():] if match else paragraph[spanish.end():]
        # Door times are deliberately ignored; all advertised performance times remain.
        tail = re.sub(r'\(Doors?[^)]*\)', '', tail, flags=re.I)
        times = [parse_time(item) for item in re.findall(r'\d{1,2}(?::|\.)\d{2}\s*(?:[ap]m)?', tail, re.I)]
        times = [item for item in times if item]
        occurrences.extend((event_date, item) for item in (times or [None]))
    return list(dict.fromkeys(occurrences))


def resolve_location(text):
    folded = text.casefold()
    locations = (
        (('berlin', 'konzerthaus'), 'Kleiner Saal, Konzerthaus Berlin', 'Berlin', 'DE'),
        (('new york', 'blue note jazz club'), 'Blue Note Jazz Club', 'New York', 'US'),
        (('new bedford', 'zeiterion'), 'Zeiterion Performing Arts Center', 'New Bedford', 'US'),
        (('tokyo', 'minamiaoyama'), 'Blue Note Tokyo', 'Tokyo', 'JP'),
        (('aarhus', 'musikhuset'), 'Store Sal, Musikhuset Aarhus', 'Aarhus', 'DK'),
        (('barcelona', 'palau de la música'), 'Palau de la Música Catalana', 'Barcelona', 'ES'),
        (('zürich', 'tonhalle'), 'Tonhalle Zürich', 'Zürich', 'CH'),
        (('zurich', 'tonhalle'), 'Tonhalle Zürich', 'Zürich', 'CH'),
    )
    for needles, venue, city, country_code in locations:
        if any(needle in folded for needle in needles):
            return venue, city, country_code
    return None


def parse_event_block(block, event_url, default_year):
    headings = [clean_text(item.get_text(' ', strip=True)) for item in block.find_all(['h2', 'h3'])]
    headings = [item for item in headings if item and not re.fullmatch(r'20\d{2}', item)]
    paragraphs = [clean_text(item.get_text(' ', strip=True)) for item in block.find_all('p')]
    paragraphs = [item for item in paragraphs if item]
    if not headings or not paragraphs:
        return []

    location = resolve_location('\n'.join(headings + paragraphs))
    occurrences = extract_occurrences(paragraphs, default_year)
    if not location or not occurrences:
        log_message(
            'Skipped incomplete Michel Camilo tour entry',
            event='crawler_item_skipped',
            level='warning',
            url=event_url,
            error_type='IncompleteEventData',
            error_message='Required date, venue, city, or country could not be resolved',
        )
        return []

    venue, city, country_code = location
    title = headings[0]
    if title.casefold().startswith(('kleiner saal', 'blue note')) and len(headings) > 1:
        title = headings[1]
    description = clean_text('\n'.join(headings[1:] + paragraphs)) or None
    return [{
        'title': title,
        'date': event_date,
        'url': event_url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for event_date, event_time in occurrences]


def parse_payload(payload):
    soup = BeautifulSoup(payload.get('mainContent') or '', 'html.parser')
    page_text = clean_text(soup.get_text(' ', strip=True))
    year_match = re.search(r'\b(20\d{2})\b', page_text)
    default_year = int(year_match.group(1)) if year_match else datetime.now().year
    blocks = soup.select('.sqs-block')
    records = []
    for index, block in enumerate(blocks):
        if block.get('data-block-type') != '2' or not block.find(['h2', 'h3']):
            continue
        event_url = TOUR_URL
        for following in blocks[index + 1:]:
            if following.get('data-block-type') == '2':
                break
            link = following.find('a', href=True)
            if link:
                event_url = urljoin(TOUR_URL, link['href'])
                break
        records.extend(parse_event_block(block, event_url, default_year))
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class MichelCamiloComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='michelcamilo_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(TOUR_JSON_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        return parse_payload(response.json())


def main():
    MichelCamiloComCrawler().run()


if __name__ == '__main__':
    main()
