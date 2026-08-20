import re
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://annami.fi/'
CALENDAR_URL = urljoin(SOURCE_URL, 'kalenteri.html')
SOURCE = 'Annami Hylkilä'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fi-FI,fi;q=0.9,en;q=0.7',
}

# Some older entries name a well-known institution or hall but omit its city.
# These defaults are source-specific and are used only when that exact venue is
# present in the entry.
VENUE_CITIES = {
    'Alminsali, Kansallisooppera': 'Helsinki',
    'Balderin sali': 'Helsinki',
    'Camerata Musiikkitalo': 'Helsinki',
    'Camerata-sali Musiikkitalo': 'Helsinki',
    'Kansallisooppera': 'Helsinki',
    'Musiikkitalo': 'Helsinki',
    'Savoy-teatteri': 'Helsinki',
    'Sibelius-Akatemia, R-talo, konserttisali': 'Helsinki',
    'Sibelius-Akatemia': 'Helsinki',
    'Tampere Ooppera': 'Tampere',
}

KNOWN_CITIES = {
    'Alavus', 'Düsseldorf', 'Espoo', 'Heinola', 'Helsinki', 'Ilmajoki',
    'Kannus', 'Kinnula', 'Kokkola', 'Kuopio', 'Lahti', 'Lestijärvi',
    'Lohja', 'Naantali', 'Nivala', 'Pietarsaari', 'Rauma', 'Toholampi',
    'Ullava', 'Vaasa', 'Vähäkyrö', 'Ylivieska',
}


def clean_text(value):
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value or '')
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def valid_date(year, month, day):
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def expand_dates(text):
    """Expand the explicit Finnish numeric dates at the start of an entry."""
    prefix = re.split(r'\s+[–—]\s+', text, maxsplit=1)[0]
    year_matches = list(re.finditer(r'\b(20\d{2})\b', prefix))
    if not year_matches:
        return []

    # A comma-separated list may contain independent dates, while an en-dash
    # or hyphen denotes an inclusive date range.
    found = []
    pattern = re.compile(
        r'(?<!\d)(\d{1,2})\.(?:(\d{1,2})\.)?\s*'
        r'(?:[–-]\s*(\d{1,2})\.(\d{1,2})\.)?\s*(20\d{2})'
    )
    for match in pattern.finditer(prefix):
        start_day = int(match.group(1))
        start_month = int(match.group(2) or match.group(4) or 0)
        end_day = int(match.group(3) or start_day)
        end_month = int(match.group(4) or start_month)
        year = int(match.group(5))
        start = valid_date(year, start_month, start_day)
        end = valid_date(year, end_month, end_day)
        if not start or not end or end < start or (end - start).days > 31:
            continue
        current = start
        while current <= end:
            found.append(current.isoformat())
            current += timedelta(days=1)
    return list(dict.fromkeys(found))


def start_time(text):
    match = re.search(r'\bklo\s+(\d{1,2})(?:[.:](\d{2}))?', text, re.I)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def location(text):
    parts = re.split(r'\s+[–—]\s+', text, maxsplit=1)
    if len(parts) != 2:
        return None, None
    value = re.split(r'\s{2,}|\s+(?=(?:Annami|Kokkola Opera|Keski-|Helsingin|Taite ry)\b)', parts[1])[0]
    value = value.strip(' .,;')

    # Multiple date/location pairs cannot safely be aligned unless the source
    # provides an unambiguous single location for the parsed occurrence.
    if ';' in value or re.search(r'\b\d{1,2}\.\d{1,2}\.20\d{2}\b', value):
        return None, None

    for city in sorted(KNOWN_CITIES, key=len, reverse=True):
        city_match = re.search(rf',\s*{re.escape(city)}\b', value, re.I)
        if city_match:
            venue = value[:city_match.start()].strip(' ,')
            return (venue or None), city

    normalized = re.sub(r'\s+', ' ', value)
    for venue, city in VENUE_CITIES.items():
        if normalized.casefold() == venue.casefold():
            return venue, city
    return None, None


def scrape_calendar():
    response = requests.get(CALENDAR_URL, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    records = []

    for heading in soup.find_all('h3'):
        title = clean_text(heading)
        details = heading.find_next_sibling('p')
        if not title or not details or title in {'Näytä menneet tapahtumat', 'Piilota menneet tapahtumat'}:
            continue
        description = clean_text(details)
        dates = expand_dates(description)
        venue, city = location(description)
        if not dates or not venue or not city:
            continue
        link = details.find('a', href=True)
        url = urljoin(CALENDAR_URL, link['href']) if link and link['href'] != '#' else CALENDAR_URL
        time_from = start_time(description)
        for event_date in dates:
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'DE' if city == 'Düsseldorf' else 'FI',
                'description': description or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    unique = {(r['title'], r['date'], r['time_from'], r['venue']): r for r in records}
    return sorted(unique.values(), key=lambda r: (r['date'], r['time_from'] or '', r['title']))


class AnnamiFiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='annami_fi',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FI',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            return scrape_calendar()
        except requests.RequestException as error:
            log_message(
                'Failed to scrape Annami Hylkilä calendar',
                event='crawler_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise


def main():
    AnnamiFiCrawler().run()


if __name__ == '__main__':
    main()
