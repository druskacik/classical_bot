import re
import unicodedata
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.nicolasdautricourt.com/calendrier'
SOURCE = 'Nicolas Dautricourt'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'JANVIER': 1, 'FEVRIER': 2, 'MARS': 3, 'AVRIL': 4,
    'MAI': 5, 'JUIN': 6, 'JUILLET': 7, 'AOUT': 8,
    'SEPTEMBRE': 9, 'OCTOBRE': 10, 'NOVEMBRE': 11, 'DECEMBRE': 12,
}

COUNTRIES = {
    'AFRIQUE DU SUD': 'ZA', 'ALLEMAGNE': 'DE', 'BELGIQUE': 'BE',
    'BRESIL': 'BR', 'CAYMAN ISLANDS': 'KY', 'DANEMARK': 'DK',
    'ESPAGNE': 'ES', 'ESTONIE': 'EE', 'ETATS-UNIS': 'US',
    'FINLANDE': 'FI', 'FRANCE': 'FR', 'ITALIE': 'IT',
    'MADAGASCAR': 'MG', 'PAYS-BAS': 'NL', 'POLOGNE': 'PL',
    'POLYNESIE FRANCAISE': 'PF', 'ROUMANIE': 'RO', 'ROYAUME-UNI': 'GB',
    'SUEDE': 'SE', 'SUISSE': 'CH',
}

NON_VENUE = re.compile(
    r'\b(?:festival|orchestre|ensemble|quartet|quatuor|trio|duet|duo|recital|'
    r'concerto|sonate|symphonie|projet|beethoven|brahms|bruch|chausson|elgar|'
    r'enescu|farrenc|mendelssohn|mozart|paganini|ravel|saint[- ]saens|'
    r'schubert|schumann|sibelius|tchaikovsky|ysaye)\b',
    re.I,
)

VENUE_MARKER = re.compile(
    r'\b(?:abbaye|amphitheatre|auditorium|bal blomet|basilique|cathedrale|'
    r'chateau|club|college|conservatoire|eglise|equinoxe|halle|hotel|institut|'
    r'maison|musee|opera|palais|salle|scene nationale|synagogue|temple|theatre)\b',
    re.I,
)


def clean_text(value):
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value or '')
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\ufeff', '')
    return re.sub(r'\s+', ' ', text).strip(' -–,')


def ascii_upper(value):
    normalized = unicodedata.normalize('NFKD', value)
    return ''.join(char for char in normalized if not unicodedata.combining(char)).upper()


def parse_dates(value, year):
    """Return only explicit individual dates, never inferred days inside a range."""
    normalized = ascii_upper(value).replace('1ER', '1')
    if re.search(r'\bAU\b', normalized):
        return []
    match = re.fullmatch(
        r'\s*(\d{1,2})(?:\s*(?:&|ET)\s*(\d{1,2}))?\s+'
        r'(JANVIER|FEVRIER|MARS|AVRIL|MAI|JUIN|JUILLET|AOUT|'
        r'SEPTEMBRE|OCTOBRE|NOVEMBRE|DECEMBRE)\s*',
        normalized,
    )
    if not match:
        return []
    results = []
    for day_value in (match.group(1), match.group(2)):
        if not day_value:
            continue
        try:
            results.append(date(year, MONTHS[match.group(3)], int(day_value)).isoformat())
        except ValueError:
            return []
    return results


def parse_location(value):
    location = clean_text(value)
    country_code = None
    country_match = re.search(r'\(([^()]*)\)\s*$', location)
    if country_match:
        marker = ascii_upper(country_match.group(1))
        country_code = COUNTRIES.get(marker)
        if re.fullmatch(r'\d{2,3}', marker):
            country_code = 'FR'
        location = location[:country_match.start()].strip(' ,-')
    if not country_code:
        upper = ascii_upper(location)
        for country, code in COUNTRIES.items():
            if upper == country or upper.endswith(f' {country}'):
                country_code = code
                location = re.sub(re.escape(country), '', upper, flags=re.I).strip(' ,-')
                break
    # A row naming several tour stops does not establish which date belongs to which city.
    if not country_code or re.search(r'\s[-–]\s|,\s*[A-ZÀ-ÖØ-Ý][\wÀ-ÿ-]+\s*$', location):
        return None, None
    if not location or len(location.split()) > 6:
        return None, None
    return location, country_code


def is_venue(value):
    value = clean_text(value)
    if not value or NON_VENUE.search(ascii_upper(value)):
        return False
    if re.search(r'\([^)]*(?:piano|violon|direction|violoncelle|alto|harpe)[^)]*\)', value, re.I):
        return False
    return bool(VENUE_MARKER.search(ascii_upper(value)))


def stable_url(href):
    url = urljoin(SOURCE_URL, href or '')
    parts = urlsplit(url)
    if parts.netloc.endswith('website-editor.net'):
        return SOURCE_URL
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, '')) or SOURCE_URL


def parse_calendar(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    year = None
    wrapper = soup.select_one('.dmRespRowsWrapper.dmListPage')
    if not wrapper:
        raise ValueError('Calendar content container was not found')

    for row in wrapper.find_all('div', class_='dmRespRow', recursive=False):
        # Desktop and mobile markup duplicate the catalogue. Parsing one variant avoids duplicates.
        if 'hide-for-small' not in (row.get('class') or []):
            continue
        paragraphs = [clean_text(item) for item in row.select('.dmNewParagraph p')]
        paragraphs = [item for item in paragraphs if item]
        if len(paragraphs) == 1 and re.fullmatch(r'20\d{2}', paragraphs[0]):
            year = int(paragraphs[0])
            continue
        if year is None or len(paragraphs) < 3:
            continue
        dates = parse_dates(paragraphs[0], year)
        city, country_code = parse_location(paragraphs[1])
        venue = paragraphs[2]
        if not dates or not city or not country_code or not is_venue(venue):
            continue
        details = paragraphs[3:]
        description = '\n'.join(details) or None
        title = details[0] if details else f'Nicolas Dautricourt — {venue}'
        link = row.select_one('a[href]')
        url = stable_url(link.get('href')) if link else SOURCE_URL
        for event_date in dates:
            records.append({
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
            })
    return records


class NicolasDautricourtComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nicolasdautricourt_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(SOURCE_URL, headers=HEADERS, timeout=90)
        response.raise_for_status()
        records = parse_calendar(response.text)
        if not records:
            log_message(
                'Nicolas Dautricourt calendar contained no complete concert records',
                event='crawler_empty_result',
                level='warning',
                url=SOURCE_URL,
                record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['city'], item['title']))


def main():
    NicolasDautricourtComCrawler().run()


if __name__ == '__main__':
    main()
