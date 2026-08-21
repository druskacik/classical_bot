import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.khamira.net/'
GIGS_URL = f'{SOURCE_URL}gigs'
SOURCE = 'Khamira'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9,cy;q=0.7',
}

MONTHS = {
    'january': 1,
    'february': 2,
    'march': 3,
    'april': 4,
    'may': 5,
    'june': 6,
    'july': 7,
    'august': 8,
    'september': 9,
    'october': 10,
    'november': 11,
    'december': 12,
}

LOCATIONS = {
    'Ulsan Jazz Festival, Ulsan': ('Ulsan Jazz Festival', 'Ulsan', 'KR'),
    'ACC World Music Festival, Gwangjun': ('ACC World Music Festival', 'Gwangju', 'KR'),
    'Seoul Music Week, South Korea': ('Seoul Music Week', 'Seoul', 'KR'),
    'Y Fenni / Abergavenny, Borough Theatre': ('Borough Theatre', 'Abergavenny', 'GB'),
    'Ystradgynlais, Neuadd Les / The Welfare': ('The Welfare', 'Ystradgynlais', 'GB'),
    'Caerdydd / Cardiff, Chapter': ('Chapter', 'Cardiff', 'GB'),
    'Narberth, Span Arts': ('Span Arts', 'Narberth', 'GB'),
    'Aberystwyth, Canolfan y Celfyddydau / Arts Centre': (
        'Aberystwyth Arts Centre', 'Aberystwyth', 'GB'
    ),
    'Caernarfon, Galeri': ('Galeri', 'Caernarfon', 'GB'),
    'Caergybi / Holyhead, Canolfan Ucheldre Centre': (
        'Ucheldre Centre', 'Holyhead', 'GB'
    ),
    'Yr Wyddgrug / Mold, Theatre Clwyd': ('Theatr Clwyd', 'Mold', 'GB'),
    'Y Gelli Gandryll / Hay-on-Wye, Hay Festival': ('Hay Festival', 'Hay-on-Wye', 'GB'),
    'British Council Theatre, New Delhi, India': (
        'British Council Theatre', 'New Delhi', 'IN'
    ),
    'BFlat Jazz Club, Bangalore, India': ('BFlat Jazz Club', 'Bengaluru', 'IN'),
    'Goa Jazz Festival, India': ('Goa Jazz Festival', 'Goa', 'IN'),
    'Kolkata Jazz Festival, India': ('Kolkata Jazz Festival', 'Kolkata', 'IN'),
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def valid_date(year, month, day):
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def listing_text(soup):
    text = clean_text(soup)
    start = text.find('GIGS / CYNGHERDDAU')
    end = text.find('Ar y wê / Social Media', start)
    if start < 0:
        return ''
    return text[start:end if end >= 0 else None]


def make_record(event_date, location_text):
    location = LOCATIONS.get(location_text.rstrip(' ,'))
    if not event_date or not location:
        return None
    venue, city, country_code = location
    return {
        'title': f'Khamira at {venue}',
        'date': event_date,
        'url': GIGS_URL,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': (
            'Khamira performs improvised world music combining Hindustani classical '
            'music, Welsh folk music, jazz and rock. The ensemble features sarangi, '
            'tabla, trumpet, guitar, bass and drums.'
        ),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_gigs(soup):
    text = listing_text(soup)
    records = []

    # The recent tour cards are posters without accessible dates. Parse every
    # older occurrence for which the page publishes a complete date and place.
    for match in re.finditer(
        r'(\d{2})/(\d{2})/(20\d{2})\s*-\s*([^\n]+)', text
    ):
        event_date = valid_date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        record = make_record(event_date, match.group(4))
        if record:
            records.append(record)

    # Pair the bilingual named dates with their adjacent location. Seoul is
    # the sole entry printed location-first; the remaining dates come first.
    pending_date = None
    previous_location = None
    year = None
    for line in text.splitlines():
        line = line.strip(' ,')
        if re.fullmatch(r'20\d{2}', line):
            year = int(line)
        elif line == 'AWST / AUGUST 2018':
            year = 2018
        date_matches = re.findall(
            r'(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)', line
        ) if re.search(
            r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b', line
        ) else []
        date_match = date_matches[-1] if date_matches else None
        if date_match and year:
            event_date = valid_date(
                year, MONTHS.get(date_match[1].lower(), 0), int(date_match[0])
            )
            if previous_location == 'Seoul Music Week, South Korea':
                record = make_record(event_date, previous_location)
                if record:
                    records.append(record)
                previous_location = None
            else:
                pending_date = event_date
        elif pending_date and line in LOCATIONS:
            record = make_record(pending_date, line)
            if record:
                records.append(record)
            pending_date = None
            previous_location = line
        elif line in LOCATIONS:
            previous_location = line
        elif line:
            previous_location = None

    unique = {(r['date'], r['venue'], r['city']): r for r in records}
    return sorted(unique.values(), key=lambda r: (r['date'], r['venue']))


class KhamiraNetCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='khamira_net',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(GIGS_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Khamira gigs',
                event='crawler_fetch_failed',
                level='error',
                url=GIGS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        return parse_gigs(BeautifulSoup(response.text, 'html.parser'))


def main():
    KhamiraNetCrawler().run()


if __name__ == '__main__':
    main()
