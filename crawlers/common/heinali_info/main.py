import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.heinali.info/'
AGENDA_URL = urljoin(SOURCE_URL, 'live')
SOURCE = 'Heinali'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_CODES = {
    'australia': 'AU',
    'austria': 'AT',
    'belgium': 'BE',
    'czechia': 'CZ',
    'denmark': 'DK',
    'finland': 'FI',
    'france': 'FR',
    'germany': 'DE',
    'greece': 'GR',
    'hungary': 'HU',
    'italy': 'IT',
    'lithuania': 'LT',
    'netherlands': 'NL',
    'poland': 'PL',
    'portugal': 'PT',
    'russia': 'RU',
    'slovakia': 'SK',
    'slovenia': 'SI',
    'spain': 'ES',
    'switzerland': 'CH',
    'türkiye': 'TR',
    'ukraine': 'UA',
    'uk': 'GB',
    'usa': 'US',
}

# A handful of older entries omit the country, but their cities are unambiguous.
CITY_COUNTRIES = {
    'berlin': 'DE',
    'kyiv': 'UA',
    'moscow': 'RU',
}

DATE_RE = re.compile(r'^(?:\(CANCELLED\)\s*)?(\d{1,2})\.(\d{1,2})[.,](20\d{2})\b', re.I)
LOCATION_RE = re.compile(
    r'^\s*(.+),\s*(' + '|'.join(re.escape(name) for name in COUNTRY_CODES) + r')\.\s*(.+)$',
    re.I,
)


def clean_text(value):
    value = value.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    value = re.sub(r'\s+', ' ', value).strip()
    return re.sub(r'\s+([.,])', r'\1', value)


def parse_date(match):
    try:
        return date(int(match.group(3)), int(match.group(2)), int(match.group(1))).isoformat()
    except ValueError:
        return None


def extract_venue(details):
    if re.fullmatch(r'(?:premiere:\s*)?tba\.?', details, re.I):
        return None

    # The agenda consistently puts the most specific place last: after the
    # final sentence/comma, or after the final "at" when only one clause exists.
    protected = re.sub(r'\b(St|V)\.', r'\1§', details)
    clauses = [part.strip(' ,.') for part in re.split(r'(?<=[a-z)])\.\s+', protected) if part.strip(' ,.')]
    candidate = clauses[-1].replace('§', '.') if clauses else ''
    at_parts = re.split(r'\s+at\s+', candidate, flags=re.I)
    has_at = len(at_parts) > 1
    if has_at:
        candidate = at_parts[-1].strip()
    elif len(clauses) < 2:
        return None
    if ',' in candidate:
        candidate = candidate.rsplit(',', 1)[-1].strip()

    candidate = re.sub(r'^(?:the\s+)', '', candidate, flags=re.I).strip(' ,.')
    if not candidate or candidate.lower() in {'tba', 'live', 'live stream'}:
        return None
    if len(candidate) > 100 or len(candidate.split()) > 12:
        return None
    return candidate


def extract_title(paragraph, details):
    linked_titles = [clean_text(link.get_text(' ', strip=True)) for link in paragraph.select('a[href]')]
    if len(linked_titles) == 1:
        title = max(linked_titles, key=len)
        if title:
            return title.strip(' ,.')

    protected = re.sub(r'\b(St|V)\.', r'\1§', details)
    title = re.split(r'\s+at\s+|(?<=[a-z)])\.\s+', protected, maxsplit=1, flags=re.I)[0]
    title = title.replace('§', '.')
    return re.sub(r'^premiere:\s*', '', title, flags=re.I).strip(' ,.')


def parse_paragraph(paragraph):
    text = clean_text(paragraph.get_text(' ', strip=True))
    match = DATE_RE.match(text)
    if not match:
        return None

    event_date = parse_date(match)
    remainder = text[match.end():].strip()
    location = LOCATION_RE.match(remainder)
    if location:
        city = location.group(1).strip()
        country_code = COUNTRY_CODES[location.group(2).lower()]
        details = location.group(3).strip()
    else:
        city_match = re.match(r'^([^.,]+?)\.\s*(.+)$', remainder)
        if not city_match:
            return None
        city = city_match.group(1).strip()
        country_code = CITY_COUNTRIES.get(city.lower())
        details = city_match.group(2).strip()

    venue = extract_venue(details)
    title = extract_title(paragraph, details)
    if not all((event_date, city, country_code, venue, title)):
        return None

    link = paragraph.select_one('a[href]')
    event_url = urljoin(AGENDA_URL, link['href']) if link else AGENDA_URL
    return {
        'title': title,
        'date': event_date,
        'url': event_url,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': details or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class HeinaliInfoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='heinali_info',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
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
        dedupe_subset=['title', 'date', 'venue', 'city'],
    )

    def scrape(self):
        try:
            response = requests.get(AGENDA_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Heinali agenda',
                event='crawler_fetch_failed',
                level='error',
                url=AGENDA_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for paragraph in soup.select('p'):
            record = parse_paragraph(paragraph)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (record['date'], record['city'], record['title'], record['url']),
        )


def main():
    HeinaliInfoCrawler().run()


if __name__ == '__main__':
    main()
