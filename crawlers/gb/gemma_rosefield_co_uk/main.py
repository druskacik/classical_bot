import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.gemma-rosefield.co.uk/'
SOURCE = 'Gemma Rosefield'
SCHEDULE_URLS = (
    urljoin(SOURCE_URL, 'concerts'),
    urljoin(SOURCE_URL, 'past-events'),
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

# The artist tours, so a home-city default would be unsafe. These names cover
# the locations published in the current and archived schedules; longer names
# must precede shorter names.
CITY_COUNTRIES = {
    'Barking and Dagenham': 'GB',
    'Stansted Mountfitchet': 'GB',
    'Bradford on Avon': 'GB',
    'Puerto de la Cruz': 'ES',
    'Santa Cruz de Tenerife': 'ES',
    'Newcastle upon Tyne': 'GB',
    'Stratford-upon-Avon': 'GB',
    'Kingston upon Hull': 'GB',
    'Great Malvern': 'GB',
    'Saffron Walden': 'GB',
    'Weston-super-Mare': 'GB',
    'Macclesfield': 'GB',
    'Huddersfield': 'GB',
    'Portsmouth': 'GB',
    'Chelmsford': 'GB',
    'Winchester': 'GB',
    'Mansfield': 'GB',
    'Sandefjord': 'NO',
    'Midhurst': 'GB',
    'Sheffield': 'GB',
    'Harrogate': 'GB',
    'Hereford': 'GB',
    'Barnsley': 'GB',
    'Bollington': 'GB',
    'Matlock': 'GB',
    'Bedford': 'GB',
    'Cardiff': 'GB',
    'London': 'GB',
    'Goole': 'GB',
    'Holt': 'GB',
    'Norwich': 'GB',
    'Leeds': 'GB',
    'Manchester': 'GB',
    'Birmingham': 'GB',
    'Oxford': 'GB',
    'Cambridge': 'GB',
    'Bristol': 'GB',
    'Edinburgh': 'GB',
    'Glasgow': 'GB',
    'Liverpool': 'GB',
    'Nottingham': 'GB',
    'Derby': 'GB',
    'York': 'GB',
    'Bath': 'GB',
    'Winchester': 'GB',
}
CITY_NAMES = sorted(CITY_COUNTRIES, key=len, reverse=True)
VENUE_DEFAULTS = {
    'Wiltshire Music Centre': ('Bradford on Avon', 'GB'),
}

DATE_RE = re.compile(
    r'^\s*(?P<day>\d{1,2})\s+'
    r'(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
    r'\s+(?P<year>20\d{2})(?P<rest>.*)$',
    re.I,
)
RANGE_RE = re.compile(r'^\s*\d{1,2}\s*(?:-|–|—)\s*\d{1,2}|^\s*\d{1,2}\s+\w+\s*(?:-|–|—)', re.I)
TIME_RE = re.compile(
    r'(?<!\d)(\d{1,2})(?::|\.)(\d{2})\s*(am|pm)?'
    r'|(?<!\d)([012]?\d)([0-5]\d)\s*(am|pm)?'
    r'|(?<!\d)(\d{1,2})\s*(am|pm)',
    re.I,
)


def clean_text(value):
    text = str(value or '').replace('\u200b', '').replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_heading(text):
    text = clean_text(text)
    if RANGE_RE.search(text):
        return None
    match = DATE_RE.match(text)
    if not match:
        return None
    try:
        date = datetime.strptime(
            f"{match.group('day')} {match.group('month')} {match.group('year')}",
            '%d %B %Y' if len(match.group('month')) > 3 else '%d %b %Y',
        ).date().isoformat()
    except ValueError:
        return None
    times = []
    for time_match in TIME_RE.finditer(match.group('rest')):
        if time_match.group(1) is not None:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            marker = (time_match.group(3) or '').lower()
        else:
            if time_match.group(4) is not None:
                hour = int(time_match.group(4))
                minute = int(time_match.group(5))
                marker = (time_match.group(6) or '').lower()
            else:
                hour = int(time_match.group(7))
                minute = 0
                marker = time_match.group(8).lower()
        if marker == 'pm' and hour < 12:
            hour += 12
        elif marker == 'am' and hour == 12:
            hour = 0
        if hour < 24 and minute < 60:
            value = f'{hour:02d}:{minute:02d}'
            if value not in times:
                times.append(value)
    return date, times or [None]


def find_location(lines):
    for index, line in enumerate(lines):
        if re.match(r'^(?:https?://|www\.)', line, re.I):
            continue
        for known_venue, (city, country_code) in VENUE_DEFAULTS.items():
            if known_venue.casefold() in line.casefold():
                return index, known_venue, city, country_code
        folded = line.casefold()
        for city in CITY_NAMES:
            if re.search(rf'(?<!\w){re.escape(city.casefold())}(?!\w)', folded):
                venue = clean_text(line)
                # Remove the city and trailing county/country/address material.
                venue = re.split(rf'\b{re.escape(city)}\b', venue, maxsplit=1, flags=re.I)[0]
                venue = clean_text(venue).rstrip(' ,:/-')
                if not venue:
                    remainder = re.split(rf'\b{re.escape(city)}\b', line, maxsplit=1, flags=re.I)[1]
                    venue = clean_text(remainder).strip(' ,:/-')
                venue = re.split(
                    r'\b(?:Road|Street|Lane|Avenue|Wellington Road|Ashley Road|Norfolk Street)\b',
                    venue,
                    maxsplit=1,
                    flags=re.I,
                )[0].strip(' ,:/-')
                if venue and venue.casefold() != city.casefold():
                    return index, venue, city, CITY_COUNTRIES[city]
    return None


def page_items(html):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.find('main') or soup
    items = []
    for element in main.select('p, h1, h2, h3, h4, h5, h6'):
        text = clean_text(element.get_text('\n', strip=True))
        if not text:
            continue
        link = element.find('a', href=True)
        href = clean_text(link['href']) if link else None
        items.append((text, urljoin(SOURCE_URL, href) if href else None))
    return items


def parse_page(html, page_url):
    items = page_items(html)
    starts = [
        index for index, (text, _) in enumerate(items)
        if parse_date_heading(text) or RANGE_RE.search(clean_text(text))
    ]
    records = []
    for position, start in enumerate(starts):
        heading = parse_date_heading(items[start][0])
        end = starts[position + 1] if position + 1 < len(starts) else len(items)
        if not heading:
            continue
        block = items[start + 1:end]
        lines = [text for text, _ in block if text]
        location = find_location(lines)
        if not heading or not location:
            continue
        location_index, venue, city, country_code = location
        links = [href for _, href in block if href and href.startswith(('http://', 'https://'))]
        url = links[-1] if links else page_url
        content = [line for index, line in enumerate(lines) if index != location_index and not re.match(r'^(?:https?://|www\.)', line, re.I)]
        if not content:
            continue
        title_seed = content[0].split('\n', 1)[0]
        title = title_seed if 'Gemma Rosefield' in title_seed else f'Gemma Rosefield – {title_seed}'
        description = '\n\n'.join(content) or None
        date, times = heading
        for time_from in times:
            records.append({
                'title': title,
                'date': date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class GemmaRosefieldCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gemma_rosefield_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        records = []
        for page_url in SCHEDULE_URLS:
            try:
                response = session.get(page_url, headers=HEADERS, timeout=45)
                response.raise_for_status()
                page_records = parse_page(response.text, page_url)
                records.extend(page_records)
                log_message(
                    'Gemma Rosefield schedule page parsed',
                    event='crawler_page_parsed',
                    url=page_url,
                    record_count=len(page_records),
                )
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Gemma Rosefield schedule page',
                    event='crawler_page_failed',
                    level='error',
                    url=page_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise
        unique = {}
        for record in records:
            key = tuple(record[field] for field in self.config.dedupe_subset)
            unique[key] = record
        return sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    GemmaRosefieldCoUkCrawler().run()


if __name__ == '__main__':
    main()
