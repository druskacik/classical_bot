import calendar
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cameratapacifica.org/'
SOURCE = 'Camerata Pacifica'
PAST_CONCERTS_URL = urljoin(SOURCE_URL, 'past-concerts/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
MONTH_PATTERN = '|'.join(calendar.month_name[1:])
WEEKDAY_PATTERN = r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)'
TIME_PATTERN = r'(?P<time>\d{1,2}(?:[.:]\d{2})?\s*[ap]\.?\s*m\.?)'
NEXT_OCCURRENCE = rf'(?=\s+{WEEKDAY_PATTERN}\b|$)'

# The site has used three occurrence formats across its published archive.
OCCURRENCE_PATTERNS = [
    re.compile(
        rf'{WEEKDAY_PATTERN}\s+(?:the\s+)?(?P<day>\d{{1,2}})\s*(?:st|nd|rd|th)?\s*,\s*'
        rf'{TIME_PATTERN}\s*[–—-]\s*(?P<place>.+?){NEXT_OCCURRENCE}',
        re.I,
    ),
    re.compile(
        rf'{WEEKDAY_PATTERN}\s*,?\s*(?P<month>{MONTH_PATTERN})\s+'
        rf'(?P<day>\d{{1,2}})\s*(?:st|nd|rd|th)?(?:\s+(?P<line_year>\d{{4}}))?'
        rf'(?:\s*,\s*\d{{4}})?\s*'
        rf'(?:,|at)\s*{TIME_PATTERN}\s*(?:[–—-]|,)\s*(?P<place>.+?){NEXT_OCCURRENCE}',
        re.I,
    ),
]

VENUE_CITIES = {
    'scherr forum': 'Thousand Oaks',
    'huntington': 'San Marino',
    'colburn school': 'Los Angeles',
    'zipper hall': 'Los Angeles',
    'music academy': 'Santa Barbara',
    'hahn hall': 'Santa Barbara',
    'santa barbara museum of natural history': 'Santa Barbara',
    'museum of ventura county': 'Ventura',
    'ventura museum': 'Ventura',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_time(value):
    match = re.fullmatch(
        r'\s*(\d{1,2})(?:[.:](\d{2}))?\s*([ap])\.?\s*m\.?\s*',
        clean_text(value),
        re.I,
    )
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.lower() == 'p' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def split_place(value):
    place = re.split(r'\s*(?:>>\s*TICKETS|“|Presented with|Please note)\b', value)[0]
    place = clean_text(place).strip(' ,.;')
    if not place:
        return '', ''

    parts = [part.strip() for part in place.split(',') if part.strip()]
    if len(parts) > 1 and parts[-1].lower() in {
        'los angeles', 'san marino', 'santa barbara', 'thousand oaks', 'ventura'
    }:
        return ', '.join(parts[:-1]), parts[-1]

    lowered = place.lower()
    for venue_fragment, city in VENUE_CITIES.items():
        if venue_fragment in lowered:
            return place, city
    return '', ''


def section_months(section_text):
    prefix = section_text[:120]
    return [MONTHS[item.lower()] for item in re.findall(MONTH_PATTERN, prefix, re.I)]


def event_title(section, year, month):
    headings = [clean_text(node) for node in section.select('h1, h2, h3, h4, h5, h6')]
    ignored = {str(year), calendar.month_name[month]}
    for heading in headings:
        if heading and heading not in ignored and not heading.isdigit():
            return heading
    return f'Camerata Pacifica – {calendar.month_name[month]} {year}'


def parse_section(section, page_url):
    description = clean_text(section)
    year_match = re.match(r'\s*(20\d{2})\b', description)
    months = section_months(description)
    if not year_match or not months:
        return []

    year = int(year_match.group(1))
    default_month_index = 0
    previous_day = None
    title = event_title(section, year, months[0])
    records = []

    # Paragraph-level parsing avoids a match consuming the following occurrence.
    lines = [clean_text(node) for node in section.select('p, li')]
    for line in lines:
        matches = sorted(
            (match for pattern in OCCURRENCE_PATTERNS for match in pattern.finditer(line)),
            key=lambda match: match.start(),
        )
        for match in matches:

            data = match.groupdict()
            day = int(data['day'])
            if data.get('month'):
                month = MONTHS[data['month'].lower()]
            else:
                if previous_day is not None and day < previous_day and default_month_index + 1 < len(months):
                    default_month_index += 1
                month = months[default_month_index]
            event_year = int(data.get('line_year') or year)
            venue, city = split_place(data['place'])
            time_from = parse_time(data['time'])
            try:
                event_date = datetime(event_year, month, day).date().isoformat()
            except ValueError:
                previous_day = day
                continue

            if venue and city and time_from:
                records.append({
                    'title': title,
                    'date': event_date,
                    'url': page_url,
                    'time_from': time_from,
                    'venue': venue,
                    'city': city,
                    'country_code': 'US',
                    'description': description,
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                })
            previous_day = day
    return records


def concert_sections(soup):
    for section in soup.select('section.elementor-top-section'):
        text = clean_text(section)
        if re.match(r'20\d{2}\b', text) and re.search(WEEKDAY_PATTERN, text, re.I):
            yield section


def season_url(soup):
    for link in soup.select('a[href]'):
        label = clean_text(link)
        href = urljoin(SOURCE_URL, link.get('href'))
        if re.fullmatch(r'Season\s+\d{2}/\d{2}', label, re.I) and href.startswith(SOURCE_URL):
            return href
    return None


def fetch_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    home_soup = fetch_soup(session, urljoin(SOURCE_URL, 'home/'))
    current_url = season_url(home_soup)
    urls = [PAST_CONCERTS_URL]
    if current_url and current_url not in urls:
        urls.insert(0, current_url)

    records = []
    for url in urls:
        soup = fetch_soup(session, url)
        page_records = []
        for section in concert_sections(soup):
            page_records.extend(parse_section(section, url))
        records.extend(page_records)
        log_message(
            'Concert page parsed',
            event='crawler_page_parsed',
            url=url,
            record_count=len(page_records),
        )

    unique = {
        (item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    result = sorted(unique.values(), key=lambda item: (item['date'], item['time_from'], item['venue']))
    if not result:
        log_message(
            'No concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return result


class CamerataPacificaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cameratapacifica_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        dedupe_subset=['date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    CamerataPacificaOrgCrawler().run()


if __name__ == '__main__':
    main()
