import re
import unicodedata
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


BASE_URL = 'https://concentus-moraviae.cz'
SOURCE_URL = f'{BASE_URL}/'
SOURCE = 'Concentus Moraviae'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}


def clean_text(value):
    if not value:
        return ''
    value = value.replace('\xa0', ' ').replace('\u202f', ' ')
    value = re.sub(r'[ \t\r\f\v]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def slugify(value):
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]+', '-', value.lower()).strip('-')


def get_soup(session, url):
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def discover_program_url(session):
    soup = get_soup(session, SOURCE_URL)
    candidates = []
    for link in soup.select('a[href]'):
        url = urljoin(SOURCE_URL, link.get('href'))
        match = re.search(r'/program-(20\d{2})/?(?:$|[?#])', url)
        if match:
            candidates.append((int(match.group(1)), url.split('#', 1)[0]))
    if not candidates:
        raise ValueError('No annual program page was found')
    return max(candidates)[1]


def parse_date(value, year):
    match = re.search(r'(\d{1,2})\s*/\s*(\d{1,2})', value or '')
    if not match:
        return None
    day, month = map(int, match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2}):([0-5]\d)\b', value or '')
    if not match:
        return None
    hour = int(match.group(1))
    if hour > 23:
        return None
    return f'{hour:02d}:{match.group(2)}'


def parse_location(city, venue):
    city = clean_text(city)
    venue = clean_text(venue)
    if not city or not venue:
        return None

    # This all-day cycling project spans regions in both CZ and AT and the
    # page provides neither one city nor one venue for it.
    if 'Lednicko-valtický areál' in city and 'Weinviertel' in venue:
        return None

    country_code = 'CZ'
    if re.search(r'(?:/\s*IT\b|\bItálie\b)', city, re.IGNORECASE):
        country_code = 'IT'
        city = re.sub(r'\s*/\s*IT\b.*$', '', city, flags=re.IGNORECASE)
    elif re.search(r'(?:/\s*Dolní Rakousko|,\s*Dolní Rakousko|\(AT\))', city, re.IGNORECASE):
        country_code = 'AT'
        city = re.sub(r'\s*(?:/|,)\s*Dolní Rakousko.*$', '', city, flags=re.IGNORECASE)

    # The site uses the castle name in its city slot for this event.
    if city == 'Státní hrad Pernštejn':
        city = 'Nedvědice'
        venue = f'Státní hrad Pernštejn, {venue}'

    if not city or not venue:
        return None
    return city, venue, country_code


def direct_columns(row):
    margin = row.select_one('.section_inner_margin')
    if not margin:
        return []
    return [child for child in margin.find_all('div', recursive=False) if 'wpb_column' in child.get('class', [])]


def parse_program_page(soup, program_url):
    year_match = re.search(r'/program-(20\d{2})/', program_url)
    if not year_match:
        raise ValueError(f'Cannot determine program year from {program_url}')
    year = int(year_match.group(1))
    records = []

    for header in soup.select('h4.title-holder'):
        row = header.find_parent('div', class_=lambda classes: classes and 'vc_row' in classes)
        columns = direct_columns(row) if row else []
        if len(columns) < 2:
            continue

        metadata = []
        for element in columns[0].select('h4, p'):
            text = clean_text(element.get_text('\n', strip=True))
            metadata.extend(value for value in text.splitlines() if value)
        title = clean_text(header.get_text(' ', strip=True))
        concert_date = parse_date(metadata[0] if metadata else None, year)
        time_from = parse_time(metadata[1] if len(metadata) > 1 else None)
        location = parse_location(
            metadata[2] if len(metadata) > 2 else None,
            metadata[3] if len(metadata) > 3 else None,
        )
        if not title or not concert_date or not location:
            log_message(
                'Skipping event with incomplete required fields',
                event='crawler_item_skipped',
                level='warning',
                url=program_url,
            )
            continue

        content = header.find_next_sibling('div', class_='accordion_content')
        description = clean_text(content.get_text('\n', strip=True)) if content else None
        description = description or None
        program_link = None
        for link in row.select('a[href]'):
            if clean_text(link.get_text(' ', strip=True)).lower() == 'program':
                program_link = urljoin(program_url, link.get('href'))
                break

        city, venue, country_code = location
        event_url = program_link or (
            f'{program_url}#event-{concert_date}-{slugify(title)}'
        )
        records.append({
            'title': title,
            'date': concert_date,
            'url': event_url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    return records


class ConcentusMoraviaeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='concentus_moraviae_cz',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CZ',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        program_url = discover_program_url(session)
        soup = get_soup(session, program_url)
        records = parse_program_page(soup, program_url)
        log_message(
            'Program page scraped',
            event='crawler_scrape_completed',
            url=program_url,
            record_count=len(records),
        )
        return records


def main():
    ConcentusMoraviaeCrawler().run()


if __name__ == '__main__':
    main()
