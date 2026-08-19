import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://hnk-osijek.hr/'
PROGRAM_URL = urljoin(SOURCE_URL, 'program/?program_id=18')
SCHEDULE_URL = urljoin(SOURCE_URL, 'raspored/?schedule_id=18')
SOURCE = 'Hrvatsko narodno kazalište u Osijeku'
HOME_CITY = 'Osijek'
HOME_VENUE = 'Hrvatsko narodno kazalište u Osijeku'

# Cloudflare currently challenges ordinary browser and requests user agents,
# while allowing the public pages to social-preview crawlers.
HEADERS = {
    'User-Agent': 'facebookexternalhit/1.1',
    'Accept-Language': 'hr-HR,hr;q=0.9,en;q=0.6',
}

MONTHS = {
    'siječnja': 1,
    'veljače': 2,
    'ožujka': 3,
    'travnja': 4,
    'svibnja': 5,
    'lipnja': 6,
    'srpnja': 7,
    'kolovoza': 8,
    'rujna': 9,
    'listopada': 10,
    'studenoga': 11,
    'prosinca': 12,
}

TOUR_LOCATIONS = {
    'zagrebu': ('Hrvatsko narodno kazalište u Zagrebu', 'Zagreb'),
    'rijeci': ('Hrvatsko narodno kazalište Ivana pl. Zajca', 'Rijeka'),
    'varaždinu': ('Hrvatsko narodno kazalište u Varaždinu', 'Varaždin'),
    'splitu': ('Hrvatsko narodno kazalište Split', 'Split'),
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_croatian_date(value):
    match = re.search(
        r'\b(\d{1,2})\.\s*([a-zčćđšž]+)\s+(\d{4})\.',
        value.lower(),
    )
    if not match or match.group(2) not in MONTHS:
        return None
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', value)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def detail_links(session):
    soup = get_soup(session, PROGRAM_URL)
    return sorted(
        {
            urljoin(SOURCE_URL, link['href'])
            for link in soup.select('.program__item a[href*="/show/"]')
            if link.get('href')
        }
    )


def scheduled_locations(session):
    """Return authoritative location overrides for explicitly touring entries."""
    soup = get_soup(session, SCHEDULE_URL)
    locations = {}
    for item in soup.select('.schedule__item'):
        date_node = item.select_one('.schedule__date__full-date')
        title_node = item.select_one('.schedule__info__title')
        link = item.select_one('a[href*="/show/"]')
        if not date_node or not title_node or not link:
            continue
        event_date = parse_croatian_date(clean_text(date_node))
        title = clean_text(title_node).lower()
        if not event_date:
            continue
        location = (HOME_VENUE, HOME_CITY)
        if 'gostovanj' in title:
            location = None
            for marker, candidate in TOUR_LOCATIONS.items():
                if marker in title:
                    location = candidate
                    break
        locations[(urljoin(SOURCE_URL, link['href']), event_date)] = location
    return locations


def explicit_detail_locations(soup):
    """Extract date/location pairs when prose explicitly lists outdoor dates."""
    text = clean_text(soup.select_one('.single__ensemble'))
    locations = {}
    if not text:
        return locations
    patterns = (
        (
            r'(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\.\s*'
            r'u\s+Vinkovcima\s+na\s+Trgu Vinkovačkih jeseni',
            ('Trg Vinkovačkih jeseni', 'Vinkovci'),
        ),
        (
            r'(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\.\s*'
            r'na\s+Trgu slobode\s+u\s+Osijeku',
            ('Trg slobode', HOME_CITY),
        ),
    )
    for pattern, location in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            try:
                event_date = date(
                    int(match.group(3)), int(match.group(2)), int(match.group(1))
                ).isoformat()
            except ValueError:
                continue
            locations[event_date] = location
    return locations


def detail_records(soup, url, location_overrides):
    title = clean_text(soup.select_one('.single__title'))
    if not title:
        return []

    description_parts = []
    for selector in ('.single__content__description', '.single__ensemble__content'):
        value = clean_text(soup.select_one(selector))
        if value and value not in description_parts:
            description_parts.append(value)
    description = '\n\n'.join(description_parts) or None
    prose_locations = explicit_detail_locations(soup)
    records = []

    for box in soup.select('.box--date'):
        event_date = parse_croatian_date(clean_text(box.select_one('.box__date')))
        time_from = parse_time(clean_text(box.select_one('.box__day-and-time')))
        if not event_date:
            continue
        location = location_overrides.get((url, event_date), (HOME_VENUE, HOME_CITY))
        location = prose_locations.get(event_date, location)
        # An explicitly advertised tour without a resolvable venue is invalid.
        if location is None:
            continue
        venue, city = location
        records.append(
            {
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'HR',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    location_overrides = scheduled_locations(session)
    records = []
    for url in detail_links(session):
        try:
            records.extend(detail_records(get_soup(session, url), url, location_overrides))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape concert detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class HnkOsijekHrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hnk_osijek_hr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='HR',
        upload_target='potential',
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
    HnkOsijekHrCrawler().run()


if __name__ == '__main__':
    main()
