import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://harrisburgsymphony.org/'
SOURCE = 'Harrisburg Symphony Orchestra'
COUNTRY_CODE = 'US'

MASTERWORKS_URL = f'{SOURCE_URL}concerts/masterworks/'
POPS_URL = f'{SOURCE_URL}concerts/pops-2/'
SUMMER_URL = f'{SOURCE_URL}concerts/summer/'
YOUTH_URL = f'{SOURCE_URL}youth-symphony/hsyo-concerts-schedule/'
YOUNG_PERSONS_URL = f'{SOURCE_URL}education/young-persons-concert/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        ('January', 'February', 'March', 'April', 'May', 'June', 'July',
         'August', 'September', 'October', 'November', 'December'),
        start=1,
    )
}
MONTHS.update({name[:3]: number for name, number in list(MONTHS.items())})


def clean_text(value):
    return re.sub(r'\s+', ' ', value or '').strip()


def iso_date(year, month, day):
    try:
        return datetime(int(year), MONTHS[month.lower().rstrip('.')], int(day)).date().isoformat()
    except (KeyError, TypeError, ValueError):
        return None


def parse_time(value):
    value = clean_text(value).replace('.', '').upper()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def record(title, date, url, time_from, venue, city, description=None):
    if not all((title, date, url, venue, city)):
        return None
    return {
        'title': clean_text(title),
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': clean_text(venue),
        'city': clean_text(city),
        'country_code': COUNTRY_CODE,
        'description': clean_text(description) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def season_years(soup):
    match = re.search(r'(20\d{2})\s*[-–]\s*(20\d{2})', soup.get_text(' ', strip=True))
    return (int(match.group(1)), int(match.group(2))) if match else None


def scrape_series(soup, listing_url):
    years = season_years(soup)
    if not years:
        return []

    records = []
    for card in soup.select('.concerts-list__info'):
        title_node = card.select_one('.concerts-list__heading')
        date_node = card.select_one('.concerts-list__date')
        link = card.select_one('.concerts-list__heading a[href], a.concert-ticket-btn[href]')
        if not title_node or not date_node or not link:
            continue

        title = clean_text(title_node.get_text(' ', strip=True))
        date_text = clean_text(date_node.get_text(' ', strip=True))
        parts = re.findall(r'([A-Za-z]+)\s+(\d{1,2})', date_text)
        if not parts:
            continue

        for month, day in parts:
            year = years[0] if MONTHS.get(month.lower()) >= 7 else years[1]
            event_date = iso_date(year, month, day)
            weekday = datetime.fromisoformat(event_date).weekday() if event_date else None
            time_from = '19:30' if weekday == 5 else ('15:00' if weekday == 6 else None)
            item = record(title, event_date, link['href'], time_from,
                          'The Forum Auditorium', 'Harrisburg')
            if item:
                records.append(item)
    return records


def scrape_summer(soup):
    records = []
    pattern = re.compile(
        r'([^,–]+),\s*([^–]+?)\s*[–-]\s*(?:[A-Za-z]+,\s*)?'
        r'([A-Za-z]+)\s+(\d{1,2}),\s*(20\d{2})\s*,?\s*'
        r'(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)$', re.I
    )
    for heading in soup.select('main h4, #content h4'):
        heading_text = clean_text(heading.get_text(' ', strip=True))
        match = pattern.search(heading_text)
        if not match and 'Carlisle (Dickinson College)' in heading_text:
            match = re.search(
                r'(Carlisle)\s*\([^)]*\)\s*[–-]\s*(?:[A-Za-z]+,\s*)?'
                r'([A-Za-z]+)\s+(\d{1,2}),\s*(20\d{2})\s*,?\s*'
                r'(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)$',
                heading_text, re.I,
            )
            if match:
                city, month, day, year, event_time = match.groups()
                venue = 'Dickinson College Quad'
                item = record('June Shomaker HSO Concert at Summerfair',
                              iso_date(year, month, day), SUMMER_URL,
                              parse_time(event_time), venue, city,
                              'Harrisburg Symphony Orchestra 2026 Free Summer Concert Series.')
                if item:
                    records.append(item)
                continue
        if not match:
            continue
        venue, city, month, day, year, event_time = match.groups()
        city = re.sub(r'\s*\([^)]*\)\s*$', '', city).strip()

        following = []
        for node in heading.find_next_siblings():
            if node.name == 'h4':
                break
            following.append(clean_text(node.get_text(' ', strip=True)))
        context = ' '.join(following[:3])
        if re.search(r'moved to', context, re.I):
            moved = next((node for node in heading.find_next_siblings()
                          if node.name == 'p' and node.find('strong')), None)
            if moved:
                venue = clean_text(moved.find('strong').get_text(' ', strip=True))
        elif city == 'Carlisle':
            venue = 'Dickinson College Quad'

        title = ('June Shomaker HSO Concert at Summerfair'
                 if city == 'Carlisle' else 'Free Summer Concert Series')
        item = record(title, iso_date(year, month, day), SUMMER_URL,
                      parse_time(event_time), venue, city,
                      'Harrisburg Symphony Orchestra 2026 Free Summer Concert Series.')
        if item:
            records.append(item)
    return records


def scrape_youth(soup):
    text = clean_text((soup.select_one('main') or soup).get_text(' ', strip=True))
    pattern = re.compile(
        r'((?:HSYO|JYSO|ESO)(?:/(?:HSYO|JYSO|ESO))*\s+(?:Fall|Winter|Spring)\s+Concert),'
        r'\s*[A-Za-z]+,\s*([A-Za-z]+)\s+'
        r'(\d{1,2}),\s*(20\d{2})\s+at\s+(\d{1,2}(?::\d{2})?\s*[ap]m)\s+'
        r'(PARMER HALL|THE FORUM)', re.I
    )
    records = []
    for title, month, day, year, event_time, venue_text in pattern.findall(text):
        venue = 'Parmer Hall' if venue_text.upper() == 'PARMER HALL' else 'The Forum Auditorium'
        city = 'Annville' if venue_text.upper() == 'PARMER HALL' else 'Harrisburg'
        item = record(title, iso_date(year, month, day), YOUTH_URL,
                      parse_time(event_time), venue, city)
        if item:
            records.append(item)
    return records


def scrape_young_persons(soup):
    main = soup.select_one('main') or soup
    text = clean_text(main.get_text(' ', strip=True))
    description = text[:text.find('Section Menu', 20)] if 'Section Menu' in text[20:] else text
    pattern = re.compile(
        r'(?:Friday|Thursday),\s*([A-Za-z]+)\.?\s+(\d{1,2}),\s*(20\d{2})\s+'
        r'at\s+(\d{1,2}(?::\d{2})?\s*[AP]M)\s+and\s+'
        r'(\d{1,2}(?::\d{2})?\s*[AP]M)', re.I
    )
    records = []
    for month, day, year, first_time, second_time in pattern.findall(text):
        for event_time in (first_time, second_time):
            item = record("Young Persons' Concert", iso_date(year, month, day),
                          YOUNG_PERSONS_URL, parse_time(event_time),
                          'The Forum Auditorium', 'Harrisburg', description)
            if item:
                records.append(item)
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    records = []
    for url in (MASTERWORKS_URL, POPS_URL):
        records.extend(scrape_series(fetch_soup(session, url), url))
    records.extend(scrape_summer(fetch_soup(session, SUMMER_URL)))
    records.extend(scrape_youth(fetch_soup(session, YOUTH_URL)))
    records.extend(scrape_young_persons(fetch_soup(session, YOUNG_PERSONS_URL)))

    if not records:
        log_message('No concerts found', event='crawler_empty_listing', level='warning',
                    url=SOURCE_URL, record_count=0)
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class HarrisburgSymphonySecureForceComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='harrisburgsymphony_secure_force_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    HarrisburgSymphonySecureForceComCrawler().run()


if __name__ == '__main__':
    main()
