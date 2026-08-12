import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://solistiveneti.it/'
UPCOMING_URL = f'{SOURCE_URL}stagioni/calendario/'
ARCHIVE_URL = f'{UPCOMING_URL}?eventi=all'
SOURCE = 'I Solisti Veneti'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}

# The calendar belongs to an Italian ensemble, but its archive also contains
# touring dates. These are the foreign locations present in the first-party
# archive; parenthesised Italian province abbreviations deliberately remain IT.
FOREIGN_LOCATIONS = {
    'kallithea - atene': ('Atene', 'GR'),
    'la chaise-dieu': ('La Chaise-Dieu', 'FR'),
    'lubiana': ('Lubiana', 'SI'),
    'madrid': ('Madrid', 'ES'),
    'madrid (e)': ('Madrid', 'ES'),
    'martigny (ch)': ('Martigny', 'CH'),
    'bragança (p)': ('Bragança', 'PT'),
    'meldorf': ('Meldorf', 'DE'),
    'amburgo': ('Amburgo', 'DE'),
    'varna': ('Varna', 'BG'),
    'almada, lisbona': ('Almada', 'PT'),
    'muscat (oman)': ('Muscat', 'OM'),
    'mendrisio': ('Mendrisio', 'CH'),
    'toulon (fr)': ('Toulon', 'FR'),
    'klagenfurt (a)': ('Klagenfurt', 'AT'),
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.fullmatch(r'(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})', clean_text(value))
    if not match:
        return None
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(2).casefold()], int(match.group(1))
        ).isoformat()
    except (KeyError, ValueError):
        return None


def parse_time(value):
    match = re.fullmatch(r'(\d{1,2})(?:[.:](\d{2}))?', clean_text(value))
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def parse_location(value):
    location = clean_text(value)
    if not location:
        return None
    folded = location.casefold()
    if folded in FOREIGN_LOCATIONS:
        return FOREIGN_LOCATIONS[folded]
    if re.search(r'\(\s*slo\s*\)', location, re.I):
        return re.sub(r'\s*\(\s*SLO\s*\)\s*', '', location, flags=re.I).strip(), 'SI'

    # Commas on this calendar sometimes introduce a street address, not a
    # second city (for example "Strà, Via Doge Pisani 7").
    city = location.split(',', 1)[0].strip()
    city = re.sub(r'\s*\([A-Z]{2}\)\s*$', '', city, flags=re.I).strip()
    return (city, 'IT') if city else None


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def detail_description(session, url):
    soup = get_soup(session, url)
    parts = []
    for node in soup.select('.EventDesc'):
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


class SolistivenetiItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='solistiveneti_it',
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
            soups = [get_soup(session, UPCOMING_URL), get_soup(session, ARCHIVE_URL)]
        except requests.RequestException as error:
            log_message(
                'Failed to fetch I Solisti Veneti calendar',
                event='crawler_fetch_failed',
                level='error',
                url=UPCOMING_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records_by_url = {}
        for row in (row for soup in soups for row in soup.select('.SingleEventRow')):
            link = row.select_one('a[href*="/eventi/"]')
            title = clean_text(row.select_one('.EventTitle'))
            event_date = parse_date(row.select_one('.EventStartDate'))
            time_from = parse_time(row.select_one('.EventStartHour'))
            venue = clean_text(row.select_one('.EventLuogo'))
            location = parse_location(row.select_one('.EventPaese'))
            url = link.get('href', '').strip() if link else ''
            if not all((title, event_date, url, venue, location)):
                continue
            city, country_code = location
            subtitle = clean_text(row.select_one('.EventSubTitle'))
            records_by_url[url] = {
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': subtitle or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }

        records = list(records_by_url.values())

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(detail_description, session, record['url']): record
                for record in records
            }
            for future in as_completed(futures):
                record = futures[future]
                try:
                    detail = future.result()
                    if detail:
                        subtitle = record['description']
                        record['description'] = (
                            f'{subtitle}\n\n{detail}' if subtitle and subtitle not in detail else detail
                        )
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch I Solisti Veneti event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=record['url'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    SolistivenetiItCrawler().run()


if __name__ == '__main__':
    main()
