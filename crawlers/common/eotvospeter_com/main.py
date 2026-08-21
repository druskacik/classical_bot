import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://eotvospeter.com/'
SOURCE = 'Peter Eötvös'
AGENDA_START_YEAR = 1946
AGENDA_CATEGORIES = ('compositions-in-concert', 'conducted-concerts')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_CODES = {
    'argentina': 'AR', 'australia': 'AU', 'austria': 'AT', 'belgium': 'BE',
    'brazil': 'BR', 'canada': 'CA', 'china': 'CN', 'croatia': 'HR',
    'czech republic': 'CZ', 'czechia': 'CZ', 'denmark': 'DK', 'estonia': 'EE',
    'finland': 'FI', 'france': 'FR', 'georgia': 'GE', 'germany': 'DE',
    'greece': 'GR', 'hong kong': 'HK', 'hungary': 'HU', 'iceland': 'IS',
    'ireland': 'IE', 'israel': 'IL', 'italy': 'IT', 'japan': 'JP',
    'latvia': 'LV', 'lithuania': 'LT', 'luxembourg': 'LU', 'mexico': 'MX',
    'monaco': 'MC', 'netherlands': 'NL', 'new zealand': 'NZ', 'norway': 'NO',
    'poland': 'PL', 'portugal': 'PT', 'romania': 'RO', 'russia': 'RU',
    'serbia': 'RS', 'singapore': 'SG', 'slovakia': 'SK', 'slovenia': 'SI',
    'south korea': 'KR', 'spain': 'ES', 'sweden': 'SE', 'switzerland': 'CH',
    'taiwan': 'TW', 'turkey': 'TR', 'uk': 'GB', 'united kingdom': 'GB',
    'usa': 'US', 'united states': 'US',
}

VENUE_PATTERN = re.compile(
    r'\b(?:academy of music|auditori(?:um|o)|basilica|cathedral|chapel|church|'
    r'concert hall|conservator(?:y|ium)|cultural cent(?:er|re)|elbtunnel|'
    r'festspielhaus|festival hall|grand th[eé]âtre|haus der musik|konzerthaus|'
    r'konserthus|kulturhaus|kunsthaus|muse(?:um|o)|music cent(?:er|re)|'
    r'opera(?: house)?|opernhaus|palace|philharmoni(?:c|e|a)|salle|staatsoper|'
    r'teatro|theat(?:er|re)|théâtre|auditorium|müpa|mupa|barbican|wigmore hall|'
    r'carnegie hall|eiffel art studios|liszt ferenc academy)\b',
    re.I,
)
NON_VENUE_PATTERN = re.compile(
    r'(?:^(?:conductor|director|orchestra|ensemble|soloist|cast|libretto|programme|'
    r'program|production|musical director|set design|light|costumes?)\s*:|'
    r'\b(?:cooperation with|students from|production)\b)', re.I
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_place(value):
    parts = [clean_text(part) for part in clean_text(value).rsplit(',', 1)]
    if len(parts) != 2:
        return '', '', ''
    city, country = parts
    return city, country, COUNTRY_CODES.get(country.casefold(), '')


def extract_venue(description):
    lines = [clean_text(line).strip(' -–—,.;') for line in description.splitlines()]
    candidates = [
        line for line in lines
        if line and len(line) <= 80 and VENUE_PATTERN.search(line)
        and not NON_VENUE_PATTERN.search(line)
    ]
    return candidates[-1] if candidates else ''


def extract_time(description):
    match = re.search(r'(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)', description)
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2)}'


def parse_event(node, page_url):
    date_place = node.select_one('.event-date-place')
    title_node = node.select_one('.event-title > strong')
    detail = node.select_one('.toggle_container')
    event_id = clean_text(detail.get('id')) if detail else ''
    if not date_place or not title_node or not detail or not event_id:
        return None

    date_node = date_place.find('strong')
    raw_date = clean_text(date_node.get_text()) if date_node else ''
    place_text = clean_text(date_place.get_text('\n', strip=True))
    place = place_text.removeprefix(raw_date).strip()
    city, _country, country_code = parse_place(place)
    title = clean_text(title_node.get_text())
    description = clean_text(detail)
    venue = extract_venue(description)

    try:
        event_date = datetime.strptime(raw_date, '%d %b, %Y').date().isoformat()
        date.fromisoformat(event_date)
    except ValueError:
        return None

    if not all((title, city, country_code, venue)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': f'{page_url}#{event_id}',
        'time_from': extract_time(description),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description or None,
    }


def scrape_page(session, year, category):
    page_url = urljoin(SOURCE_URL, f'agenda/{year}/{category}/')
    response = session.get(page_url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    records = []
    for node in soup.select('.agenda-month-box-list.toggle-trigger'):
        record = parse_event(node, page_url)
        if record:
            records.append(record)
        else:
            detail = node.select_one('.toggle_container')
            log_message(
                'Skipped incomplete Peter Eötvös agenda event',
                event='crawler_item_skipped',
                level='warning',
                url=f"{page_url}#{clean_text(detail.get('id')) if detail else ''}",
                error_type='IncompleteEventData',
                error_message='Required date, title, venue, city, or country is missing',
            )
    return records


class EotvospeterComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='eotvospeter_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        jobs = [
            (year, category)
            for year in range(AGENDA_START_YEAR, date.today().year + 1)
            for category in AGENDA_CATEGORIES
        ]
        records = []
        failures = []
        # This WordPress installation becomes unreliable under heavier bursts.
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(scrape_page, session, year, category): (year, category)
                for year, category in jobs
            }
            for future in as_completed(futures):
                year, category = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    failures.append((year, category, error))
                    log_message(
                        'Failed to fetch Peter Eötvös agenda page',
                        event='crawler_page_failed',
                        level='warning',
                        url=urljoin(SOURCE_URL, f'agenda/{year}/{category}/'),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        if failures:
            raise RuntimeError(
                f'Failed to fetch {len(failures)} Peter Eötvös agenda pages; '
                'refusing to return a partial archive'
            )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    EotvospeterComCrawler().run()


if __name__ == '__main__':
    main()
