import re
from datetime import date, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ingolfwunder.com/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts/')
SOURCE = 'Ingolf Wunder'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_CODES = {
    'Argentina': 'AR',
    'Armenia': 'AM',
    'Austria': 'AT',
    'Bulgaria': 'BG',
    'China': 'CN',
    'Finland': 'FI',
    'France': 'FR',
    'Germany': 'DE',
    'Hungary': 'HU',
    'Israel': 'IL',
    'Italy': 'IT',
    'Malta': 'MT',
    'Poland': 'PL',
    'Portugal': 'PT',
    'Russia': 'RU',
    'Saudi Arabia': 'SA',
    'Slovakia': 'SK',
    'South Korea': 'KR',
    'United States': 'US',
}

DATE_PATTERN = re.compile(
    r'^(?P<month>[A-Za-z]+)\s+(?P<start>\d{1,2})'
    r'(?:-(?P<end>\d{1,2}))?,\s*(?P<year>\d{4})$'
)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_dates(value):
    match = DATE_PATTERN.fullmatch(value.strip())
    if not match:
        return []
    try:
        parsed_start = date.fromisoformat(
            f"{match.group('year')}-{MONTHS[match.group('month').lower()]:02d}-"
            f"{int(match.group('start')):02d}"
        )
        parsed_end = parsed_start.replace(day=int(match.group('end') or match.group('start')))
    except (KeyError, ValueError):
        return []
    return [
        (parsed_start + timedelta(days=offset)).isoformat()
        for offset in range((parsed_end - parsed_start).days + 1)
    ]


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


def parse_location(value):
    for country, country_code in sorted(
        COUNTRY_CODES.items(), key=lambda item: len(item[0]), reverse=True
    ):
        suffix = f' {country}'
        if value.casefold().endswith(suffix.casefold()):
            city = value[:-len(suffix)].strip()
            return (city, country_code) if city else (None, None)
    return None, None


def usable_url(box):
    for link in box.select('a[href]'):
        candidate = urljoin(CONCERTS_URL, link.get('href', '').strip())
        parsed = urlparse(candidate)
        if parsed.scheme in {'http', 'https'} and parsed.hostname and '.' in parsed.hostname:
            return candidate
    return CONCERTS_URL


def parse_venue(value):
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    venue = lines[0] if lines else ''
    # A few cards put the event/festival name before the actual building.
    for separator in (', ', ' - '):
        tail = venue.rsplit(separator, 1)[-1].strip()
        if re.search(r'\b(?:hall|theatre|opera|saal|center|centre)\b', tail, re.I):
            venue = tail
    if re.search(
        r'\b(?:to be announced|tba|festival|summit|platform|presentation)\b',
        venue,
        re.I,
    ) or venue.casefold() in {'cremona musica', 'musiksommer'}:
        return None, []
    return venue or None, lines[1:]


def parse_box(box):
    headings = [clean_text(element) for element in box.select('h5, h3')]
    if len(headings) < 2:
        return []
    event_dates = parse_dates(headings[0])
    city, country_code = parse_location(headings[1])
    details = [
        clean_text(element) for element in box.select('.btx-text-content-inner')
    ]
    details = [value for value in details if value]
    if not event_dates or not city or not country_code or len(details) < 2:
        return []

    venue, extra_description = parse_venue(details[0])
    if not venue:
        return []
    title = details[1]
    description_parts = extra_description + details[1:]
    description = '\n\n'.join(description_parts) or None
    url = usable_url(box)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': None,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in event_dates
    ]


class IngolfwunderComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ingolfwunder_com',
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
            response = requests.get(CONCERTS_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Ingolf Wunder concerts',
                event='crawler_fetch_failed',
                level='error',
                url=CONCERTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for box in soup.select('.btx-item.js-item-box'):
            records.extend(parse_box(box))
        return sorted(
            records,
            key=lambda item: (item['date'], item['city'], item['title'], item['url']),
        )


def main():
    IngolfwunderComCrawler().run()


if __name__ == '__main__':
    main()
