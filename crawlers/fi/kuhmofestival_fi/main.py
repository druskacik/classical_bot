import re
from datetime import datetime
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://kuhmofestival.fi/'
PROGRAM_URL = f'{SOURCE_URL}ohjelma/'
SOURCE = 'Kuhmon Kamarimusiikki'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fi-FI,fi;q=0.9,en;q=0.7',
}

# Almost all festival venues are in Kuhmo. These are the explicitly named
# performances in nearby municipalities on the same programme page.
TOURING_VENUES = {
    'iisalmen nuokkari': 'Iisalmi',
    'vesantalo': 'Vesanto',
    'sotkamon kirkko': 'Sotkamo',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(r'\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b', value)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(0), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None


def parse_heading(value):
    """Return the optional concert number, time, and venue from a heading."""
    match = re.match(
        r'^\s*(?:(\d+)\.\s+)?(\d{1,2})[.:](\d{2})\s+(.+?)'
        r'(?:\s+[—–]\s+.*)?\s*$',
        value,
    )
    if not match:
        return None, None, None
    number, hour, minute, venue = match.groups()
    if int(hour) > 23 or int(minute) > 59:
        return None, None, None
    return number, f'{int(hour):02d}:{minute}', clean_text(venue).rstrip('* ').strip()


def resolve_city(venue):
    normalized = venue.casefold()
    for token, city in TOURING_VENUES.items():
        if token in normalized:
            return city
    return 'Kuhmo'


def concert_title(concert, number):
    title = clean_text(concert.select_one('.concert-name'))
    if title:
        return title
    first_work = clean_text(concert.select_one('.program-name'))
    if first_work:
        return first_work
    return f'Konsertti {number}' if number else ''


def parse_concert(concert):
    event_date = parse_date(clean_text(concert.select_one('.concert-date')))
    heading = clean_text(concert.select_one('.concert-title')).replace('\n', ' ')
    number, time_from, venue = parse_heading(heading)
    title = concert_title(concert, number)
    if not all((event_date, time_from, venue, title)):
        return None

    anchor = concert.select_one('.concert-title').get('id', '')
    query = urlencode({'date': event_date})
    url = f'{PROGRAM_URL}?{query}' + (f'#{anchor}' if anchor else '')
    description = clean_text(concert) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': resolve_city(venue),
        'country_code': 'FI',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts():
    response = requests.get(PROGRAM_URL, headers=HEADERS, timeout=90)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    records = []
    for concert in soup.select('.concert'):
        try:
            record = parse_concert(concert)
        except (AttributeError, TypeError, ValueError) as error:
            log_message(
                'Failed to parse Kuhmo festival concert',
                event='crawler_item_failed',
                level='warning',
                url=PROGRAM_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )


class KuhmoFestivalFiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kuhmofestival_fi',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FI',
        # The programme also contains talks, films, and other festival events.
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
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    KuhmoFestivalFiCrawler().run()


if __name__ == '__main__':
    main()
