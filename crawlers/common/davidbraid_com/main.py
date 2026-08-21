import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://davidbraid.com/'
TOUR_URL = urljoin(SOURCE_URL, 'ConcertsPremiereMasterclasses.html')
SOURCE = 'David Braid'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-CA,en;q=0.9',
}

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        ('', 'January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December')
    )
    if name
}

COUNTRIES = {
    'Argentina': 'AR', 'Armenia': 'AM', 'Australia': 'AU', 'Brazil': 'BR',
    'Canada': 'CA', 'China': 'CN', 'Czechia': 'CZ', 'Denmark': 'DK',
    'Finland': 'FI', 'France': 'FR', 'Georgia': 'GE', 'Germany': 'DE',
    'Hong Kong': 'HK', 'Italy': 'IT', 'Japan': 'JP', 'Kazakhstan': 'KZ',
    'Korea': 'KR', 'Lithuania': 'LT', 'Netherlands': 'NL', 'Norway': 'NO',
    'Russia': 'RU', 'Scotland': 'GB', 'Sweden': 'SE', 'Switzerland': 'CH',
    'Turkey': 'TR', 'Türkiye': 'TR', 'United Kingdom': 'GB', 'UK': 'GB',
    'USA': 'US', 'Uzbekistan': 'UZ',
}

NON_EVENT = re.compile(
    r'\b(recording|masterclass|residency|guest instructor|composer-in-residence|'
    r'presentation|film screening|interview|workshop|talk)\b', re.I
)
PERFORMANCE = re.compile(
    r'\b(concert|recital|premiere|performance|festival|orchestra|quartet|trio|'
    r'quintet|duo|solo|music|jazz|choir|symphony|sinfonia|album launch)\b', re.I
)
VENUE = re.compile(
    r'\b(hall|theatre|theater|auditorium|church|kirke|minster|chapel|centre|center|'
    r'conservator|university|museum|gallery|club|bistro|salon|opera house|'
    r'arts place|jazz dock|jazz bistro|kanapee|the rex|the bassment|steinway)\b', re.I
)


def clean(value):
    return re.sub(r'\s+', ' ', value or '').strip(' ,;')


def country_code(element, heading):
    image = element.select_one('img[alt]')
    value = clean(image.get('alt', '').replace('Flag', '')) if image else ''
    combined = f'{value} {heading}'
    for name, code in COUNTRIES.items():
        if re.search(rf'\b{re.escape(name)}\b', combined, re.I):
            return code
    return None


def parse_full_date(value):
    match = re.fullmatch(r'\s*(\d{1,2})\s+([A-Za-z]+)\s*,?\s*(20\d{2})\s*', value)
    if not match or match.group(2).lower() not in MONTHS:
        return None
    try:
        return date(int(match.group(3)), MONTHS[match.group(2).lower()], int(match.group(1)))
    except ValueError:
        return None


def heading_date_context(value):
    year_match = re.search(r'\b(20\d{2})\b', value)
    months = [number for name, number in MONTHS.items() if re.search(rf'\b{name}\b', value, re.I)]
    return (int(year_match.group(1)) if year_match else None, months[-1] if months else None)


def dated_itinerary_line(line, year, default_month):
    match = re.match(
        r'^(?:(\d{1,2})(?:-(\d{1,2}))?\.(\d{1,2})|'
        r'(\d{1,2})(?:-(\d{1,2}))?\s+([A-Za-z]+)|'
        r'([A-Za-z]+)\s+(\d{1,2})(?:-(\d{1,2}))?)\s*:?,?\s*(.+)$', line
    )
    if not match or not year:
        return []
    if match.group(1):
        start, end, month = int(match.group(1)), int(match.group(2) or match.group(1)), int(match.group(3))
    elif match.group(4):
        month = MONTHS.get(match.group(6).lower())
        start, end = int(match.group(4)), int(match.group(5) or match.group(4))
    else:
        month = MONTHS.get(match.group(7).lower())
        start, end = int(match.group(8)), int(match.group(9) or match.group(8))
    month = month or default_month
    if not month or end - start > 7:
        return []
    result = []
    for day in range(start, end + 1):
        try:
            result.append((date(year, month, day), clean(match.group(10))))
        except ValueError:
            pass
    return result


def location_from_text(text, heading, country):
    parts = [clean(part) for part in text.split(',') if clean(part)]
    city = None
    venue = None
    if len(parts) >= 2 and not re.search(r'\b(with|composer|piano|violin|drums)\b', parts[0], re.I):
        if VENUE.search(parts[0]) and not VENUE.search(parts[1]):
            venue, city = parts[0], parts[1]
        else:
            city, venue = parts[0], ', '.join(parts[1:])
    heading_parts = [clean(part) for part in heading.split(',')]
    if not city and len(heading_parts) > 1:
        city = heading_parts[1]
    if not venue and VENUE.search(text):
        venue = re.sub(r'^.*?\bat\s+', '', text, flags=re.I)
        venue = re.sub(r'^.*?:\s*', '', venue)
        if city and venue.lower().startswith(city.lower() + ','):
            venue = clean(venue[len(city) + 1:])
    if venue and country:
        venue = re.sub(rf',?\s*{re.escape(country)}\s*$', '', venue, flags=re.I)
    if venue:
        venue = re.sub(r'^.*?\b(?:concert|recital|performance)\s+at\s+', '', venue, flags=re.I)
        if re.fullmatch(r'.*\b(?:concert|recital|performance|premiere)\b', venue, re.I):
            venue = ''
    return clean(city), clean(venue)


def parse_element(element):
    date_node = element.select_one('.mbr-timeline-date')
    title_node = element.select_one('.mbr-timeline-title')
    body_node = element.select_one('.mbr-text')
    if not date_node or not title_node or not body_node:
        return []
    date_text = clean(date_node.get_text(' ', strip=True))
    heading = clean(title_node.get_text(' ', strip=True))
    lines = [clean(line) for line in body_node.get_text('\n').splitlines() if clean(line)]
    body = '\n'.join(lines)
    code = country_code(element, heading)
    if not code:
        return []
    link = body_node.select_one('a[href]')
    url = urljoin(TOUR_URL, link['href']) if link else TOUR_URL
    year, month = heading_date_context(date_text)
    country_name = next((name for name, value in COUNTRIES.items() if value == code), '')
    records = []

    itinerary = []
    for line in lines:
        itinerary.extend(dated_itinerary_line(line, year, month))
    if itinerary:
        for event_date, text in itinerary:
            if NON_EVENT.search(text) and not re.search(r'\b(concert|recital|performance|premiere)\b', text, re.I):
                continue
            city, venue = location_from_text(text, heading, country_name)
            if not city or not venue:
                continue
            records.append(make_record(text, event_date, url, venue, city, code, body))
        return records

    event_date = parse_full_date(date_text)
    if not event_date or (NON_EVENT.search(body) and not re.search(
        r'\b(concert|recital|performance|premiere)\b', body, re.I
    )):
        return []
    city = ''
    venue = ''
    heading_parts = [clean(part) for part in heading.split(',')]
    if len(heading_parts) > 1:
        city = heading_parts[1]
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        match = re.match(r'^([^,]+),\s*(?:[A-Z]{2}|Canada|USA|UK|Czechia|Denmark|Germany)$', line, re.I)
        if match:
            city = clean(match.group(1))
            if index and VENUE.search(lines[index - 1]):
                venue = lines[index - 1]
            break
    if not venue:
        venue = next((line for line in reversed(lines) if len(line) <= 90 and VENUE.search(line)), '')
    venue = re.split(r',\s*(?:Music by|with|featuring)\b', venue, maxsplit=1, flags=re.I)[0]
    if not city or not venue:
        return []
    title = next((line for line in lines if line.lower() not in {'tickets/info', 'more information'}), heading)
    records.append(make_record(title, event_date, url, venue, city, code, body))
    return records


def make_record(title, event_date, url, venue, city, code, description):
    return {
        'title': clean(title),
        'date': event_date.isoformat(),
        'url': url,
        'time_from': None,
        'venue': clean(venue),
        'city': clean(city),
        'country_code': code,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class DavidBraidComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='davidbraid_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'venue', 'city'],
    )

    def scrape(self):
        try:
            response = requests.get(TOUR_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch David Braid tour archive',
                event='crawler_fetch_failed',
                level='error',
                url=TOUR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        soup = BeautifulSoup(response.content, 'html.parser')
        records = []
        for element in soup.select('.timeline-element'):
            records.extend(parse_element(element))
        return sorted(records, key=lambda item: (item['date'], item['city'], item['title']))


def main():
    DavidBraidComCrawler().run()


if __name__ == '__main__':
    main()
