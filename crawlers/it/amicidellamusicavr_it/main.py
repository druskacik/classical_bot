import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.amicidellamusicavr.it/'
CALENDAR_URL = urljoin(SOURCE_URL, 'programma-eventi-e-concerti/all')
ORGAN_CALENDAR_URL = 'https://www.organistoriciverona.it/concerti'
SOURCE = 'Società Amici della Musica di Verona'
YEAR = 2026

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gen': 1, 'gennaio': 1, 'feb': 2, 'febbraio': 2, 'mar': 3, 'marzo': 3,
    'apr': 4, 'aprile': 4, 'mag': 5, 'maggio': 5, 'giu': 6, 'giugno': 6,
    'lug': 7, 'luglio': 7, 'ago': 8, 'agosto': 8, 'set': 9, 'settembre': 9,
    'ott': 10, 'ottobre': 10, 'nov': 11, 'novembre': 11,
    'dic': 12, 'dicembre': 12,
}

DATE_RE = re.compile(
    r'(?im)^(?:lunedì|martedì|mercoledì|giovedì|venerdì|sabato|domenica)\s+'
    r'(\d{1,2})\s+([a-zà]+)(?:\s+(20\d{2}))?(?:\s*-\s*ore\s*(\d{1,2})[.:](\d{2}))?\s*$'
)
LOCAL_DATE_RE = re.compile(
    r'(?im)^(?:(?:lunedì|martedì|mercoledì|giovedì|venerdì|sabato|domenica)\s+)?'
    r'(\d{1,2})\s+([a-zà]+)(?:\s+(20\d{2}))?(?:\s+ore\s*(\d{1,2})[.:](\d{2}))?'
    r'(?:\s*\([^\n]*\))?\s*$'
)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\ufeff', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def iso_date(day, month_name, year):
    month = MONTHS[month_name.casefold().rstrip('.')]
    return date(int(year), month, int(day)).isoformat()


def base_record(title, event_date, url, time_from, venue, city, description):
    return {
        'title': clean_text(title),
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': clean_text(venue),
        'city': clean_text(city),
        'country_code': 'IT',
        'description': clean_text(description) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_detail(soup, url):
    content = soup.select_one('.post-content')
    info = soup.select_one('.post-info-container')
    if content is None or info is None:
        return None

    title = clean_text(info.select_one('h1'))
    date_node = info.select_one('.meta-user')
    venue_link = info.select_one('.fa-map-marker')
    venue_link = venue_link.find_parent('a') if venue_link else None
    clock = info.select_one('.fa-clock-o')
    clock_node = clock.find_parent('li') if clock else None
    match = DATE_RE.search(clean_text(date_node))
    if not title or match is None or venue_link is None:
        return None

    event_date = iso_date(match.group(1), match.group(2), match.group(3))
    venue = clean_text(venue_link)
    time_match = re.search(r'\b(\d{1,2}):(\d{2})\b', clean_text(clock_node))
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    city = 'Verona'
    description_parts = []
    for child in content.find_all(recursive=False):
        if 'post-info-container' in (child.get('class') or []) or 'share-post' in (child.get('class') or []):
            continue
        text = clean_text(child)
        if text:
            description_parts.append(text)
    return base_record(title, event_date, url, time_from, venue, city, '\n\n'.join(description_parts))


def series_title(segment, fallback):
    ignored = re.compile(r'^(?:ore\b|musiche?\b|programma\b|tutti i concerti|ingresso\b)', re.I)
    lines = clean_text(segment).splitlines()
    for index, line in enumerate(lines):
        line = line.strip(' -')
        if line and not ignored.search(line) and not re.match(r'^[A-Z. ]+\s*\(\d{4}', line):
            if line.upper() == line and index + 1 < len(lines):
                following = lines[index + 1].strip(' -')
                if following and following.upper() == following and not ignored.search(following):
                    return f'{line} {following}'
            return line
    return fallback


def parse_local_series(soup, url, venue, fallback_title):
    content = soup.select_one('.post-content')
    if content is None:
        return []
    text = clean_text(content)
    matches = list(LOCAL_DATE_RE.finditer(text))
    records = {}
    for index, match in enumerate(matches):
        year = match.group(3) or str(YEAR)
        event_date = iso_date(match.group(1), match.group(2), year)
        segment = text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)]
        time_match = re.search(r'(?i)\bore\s*(\d{1,2})[.:](\d{2})', segment)
        time_from = (
            f'{int(match.group(4)):02d}:{match.group(5)}' if match.group(4)
            else f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match
            else '21:00'
        )
        record = base_record(
            series_title(segment, fallback_title), event_date, url, time_from,
            venue, 'Verona', segment,
        )
        # Overview headers repeat the first date before the actual programme.
        # The later occurrence is the concrete, more useful programme block.
        records[event_date] = record
    return list(records.values())


def organ_city(venue):
    mappings = (
        ('Desenzano del Garda', 'Desenzano del Garda'), ('Malcesine', 'Malcesine'),
        ('Pesina', 'Caprino Veronese'), ('Peschiera del Garda', 'Peschiera del Garda'),
        ('Parona', 'Verona'), ('Sant’Ambrogio', 'Sant\'Ambrogio di Valpolicella'),
        ('Fumane', 'Fumane'), ('Erbezzo', 'Erbezzo'), ('Boscochiesanuova', 'Bosco Chiesanuova'),
        ('Santa Maria in Stelle', 'Verona'), ('Soave', 'Soave'), ('Caldierino', 'Caldiero'),
        ('Cologna Veneta', 'Cologna Veneta'), ('Palazzolo di Sona', 'Sona'),
        ('Isola della Scala', 'Isola della Scala'), ('Concamarise', 'Concamarise'),
        ('Bovolone', 'Bovolone'), ('Sommacampagna', 'Sommacampagna'),
        ('Cadidavid', 'Verona'), ('Valeggio sul Mincio', 'Valeggio sul Mincio'),
        ('Nogarole Rocca', 'Nogarole Rocca'),
    )
    for needle, city in mappings:
        if needle.casefold() in venue.casefold():
            return city
    if any(name in venue for name in ('San Nicolò', 'Santa Maria in Organo', 'San Tomaso',
                                       'San Pietro in Monastero', 'Cattedrale', 'San Bernardino')):
        return 'Verona'
    return None


def organ_title(segment):
    for line in clean_text(segment).splitlines()[1:]:
        if re.match(r'(?i)^(?:organista|ensemble|coro|tromba|duo organistico|soprano)\s*:', line):
            return f'Festival Organi Storici – {line.split(":", 1)[1].strip()}'
        if re.match(r'(?i)^(?:ensemble|coro)\b', line):
            return f'Festival Organi Storici – {line}'
    return 'Festival Organi Storici'


def parse_organ_calendar(soup):
    text = clean_text(soup.body)
    matches = list(DATE_RE.finditer(text))
    records = []
    for index, match in enumerate(matches):
        segment = text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)]
        lines = [line.strip() for line in segment.splitlines() if line.strip()]
        if not lines:
            continue
        venue = lines[0]
        city = organ_city(venue)
        if city is None:
            continue
        event_date = iso_date(match.group(1), match.group(2), match.group(3) or YEAR)
        time_from = f'{int(match.group(4)):02d}:{match.group(5)}' if match.group(4) else None
        records.append(base_record(
            organ_title(segment), event_date, ORGAN_CALENDAR_URL, time_from,
            venue, city, segment,
        ))
    return records


class AmicidellamusicavrItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='amicidellamusicavr_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            calendar = get_soup(session, CALENDAR_URL)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Amici della Musica Verona calendar',
                event='crawler_fetch_failed', level='error', url=CALENDAR_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        links = []
        for link in calendar.select('a[href*="/programma/"]'):
            url = urljoin(SOURCE_URL, link.get('href', ''))
            if url not in links:
                links.append(url)

        records = []
        for url in links:
            try:
                soup = get_soup(session, url)
                if '/112/' in url:
                    records.extend(parse_local_series(
                        soup, url, 'Monastero degli Stimmatini di Sezano', 'SOLI 2026',
                    ))
                elif '/115/' in url:
                    records.extend(parse_local_series(
                        soup, url, 'Castello di Montorio', 'Concerti al Castello di Montorio',
                    ))
                elif '/107/' not in url:
                    record = parse_detail(soup, url)
                    if record:
                        records.append(record)
            except (requests.RequestException, KeyError, TypeError, ValueError) as error:
                log_message(
                    'Failed to parse Amici della Musica Verona programme item',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )

        if any('/107/' in url for url in links):
            try:
                records.extend(parse_organ_calendar(get_soup(session, ORGAN_CALENDAR_URL)))
            except (requests.RequestException, KeyError, TypeError, ValueError) as error:
                log_message(
                    'Failed to fetch linked Festival Organi Storici calendar',
                    event='crawler_item_failed', level='warning', url=ORGAN_CALENDAR_URL,
                    error_type=type(error).__name__, error_message=str(error),
                )

        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    AmicidellamusicavrItCrawler().run()


if __name__ == '__main__':
    main()
