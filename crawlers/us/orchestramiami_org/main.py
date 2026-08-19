import re
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.orchestramiami.org/'
SOURCE = 'Orchestra Miami'
SEASON_URL = urljoin(SOURCE_URL, 'concerts-and-events-25-26')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        ('', 'January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December')
    )
}
MONTHS.update({name[:3].lower(): number for name, number in MONTHS.copy().items()})

LOCATIONS = {
    'gateway park': ('Gateway Park', 'Sunny Isles Beach'),
    'pinecrest gardens': ('Pinecrest Gardens', 'Pinecrest'),
    'miami beach bandshell': ('Miami Beach Bandshell', 'Miami Beach'),
    'the sanctuary of the arts': ('The Sanctuary of the Arts', 'Coral Gables'),
    'sanctuary of the arts': ('The Sanctuary of the Arts', 'Coral Gables'),
    'trinity episcopal cathedral': ('Trinity Episcopal Cathedral', 'Miami'),
    'on the beach in miami beach': ('Miami Beach at 21st Street', 'Miami Beach'),
    'on the beach': ('Miami Beach at 21st Street', 'Miami Beach'),
}

DATE_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'(January|February|March|April|May|June|July|August|September|October|November|December|'
    r'Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|Sep\.?|Sept\.?|Oct\.?|Nov\.?|Dec\.?)'
    r'\s+(\d{1,2})(?:st|nd|rd|th)?(?:,)?(?:\s+(20\d{2}))?'
    r'(?:\s*(?:@|at|,)\s*(\d{1,2})(?::(\d{2}))?\s*([AP]M))?',
    re.IGNORECASE,
)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_links(soup):
    main = soup.select_one('main')
    if main is None:
        return []
    links = []
    for anchor in main.select('a[href]'):
        title = clean_text(anchor).replace('\n', ' ')
        url = urljoin(SEASON_URL, anchor.get('href', ''))
        if (
            not title
            or urlparse(url).netloc != urlparse(SOURCE_URL).netloc
            or '@' in title
            or title.upper() == 'RESCHEDULED'
            or title.lower().strip(' -') in LOCATIONS
            or re.search(r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\b', title)
        ):
            continue
        parent = anchor
        card_text = ''
        for _ in range(8):
            parent = parent.parent
            if parent is None or parent == main:
                break
            candidate = clean_text(parent)
            if DATE_RE.search(candidate):
                card_text = candidate
                break
        item = (title, url.split('#', 1)[0], card_text)
        if item not in links:
            links.append(item)
    return links


def infer_year(month, detail_text):
    years = [int(value) for value in re.findall(r'\b20\d{2}\b', detail_text)]
    season_years = [year for year in years if year in (2025, 2026)]
    if season_years:
        preferred = 2025 if month >= 8 else 2026
        if preferred in season_years:
            return preferred
    return 2025 if month >= 8 else 2026


def parse_occurrences(text):
    occurrences = []
    lowered = text.lower()
    matches = list(DATE_RE.finditer(text))
    for index, match in enumerate(matches):
        month_name = match.group(1).rstrip('.').lower()
        month = MONTHS.get(month_name) or MONTHS.get(month_name[:3])
        year = int(match.group(3)) if match.group(3) else infer_year(month, text)
        if year not in (2025, 2026):
            continue
        try:
            event_date = date(year, month, int(match.group(2))).isoformat()
        except (TypeError, ValueError):
            continue

        hour = int(match.group(4)) if match.group(4) else None
        minute = int(match.group(5) or 0) if hour is not None else None
        if hour is not None:
            if match.group(6).upper() == 'PM' and hour != 12:
                hour += 12
            if match.group(6).upper() == 'AM' and hour == 12:
                hour = 0
        time_from = f'{hour:02d}:{minute:02d}' if hour is not None else None

        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        before = lowered[max(0, match.start() - 100):match.start()]
        location = None
        if re.search(r'-\s*$', before):
            preceding = [
                (before.rfind(key), value)
                for key, value in LOCATIONS.items() if key in before
            ]
            if preceding:
                location = max(preceding, key=lambda item: item[0])[1]
        nearby = lowered[match.end():min(end, match.end() + 240)]
        if location is None:
            location = next((value for key, value in LOCATIONS.items() if key in nearby), None)
        if location is None:
            location = next((value for key, value in LOCATIONS.items() if key in before), None)
        if location is None:
            page_locations = {
                value for key, value in LOCATIONS.items() if key in lowered
            }
            if len(page_locations) == 1:
                location = page_locations.pop()
        if location is None:
            continue
        occurrence = (event_date, time_from, *location)
        if occurrence not in occurrences:
            occurrences.append(occurrence)
    timed_keys = {
        (event_date, venue) for event_date, time_from, venue, _ in occurrences if time_from
    }
    return [
        occurrence for occurrence in occurrences
        if occurrence[1] or (occurrence[0], occurrence[2]) not in timed_keys
    ]


class OrchestraMiamiOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestramiami_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
            response = session.get(SEASON_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Orchestra Miami season page',
                event='crawler_fetch_failed', level='error', url=SEASON_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        links = event_links(BeautifulSoup(response.text, 'html.parser'))
        grouped = {}
        for title, url, card_text in links:
            grouped.setdefault(url, []).append((title, card_text))

        records = []
        for url, entries in grouped.items():
            try:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Orchestra Miami event page',
                    event='crawler_fetch_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            soup = BeautifulSoup(detail_response.text, 'html.parser')
            description = clean_text(soup.select_one('main')) or None
            detail_by_date = {
                occurrence[0]: occurrence
                for occurrence in parse_occurrences(description or '')
            }
            parsed_entries = []
            for title, card_text in entries:
                occurrences = parse_occurrences(card_text)
                if occurrences:
                    for occurrence in occurrences:
                        detail = detail_by_date.get(occurrence[0])
                        if detail:
                            detail = (detail[0], detail[1] or occurrence[1], detail[2], detail[3])
                        parsed_entries.append((title, detail or occurrence))
            if not parsed_entries:
                fallback_title = entries[0][0]
                parsed_entries = [
                    (fallback_title, occurrence)
                    for occurrence in parse_occurrences(description or '')
                ]
            for title, (event_date, time_from, venue, city) in parsed_entries:
                records.append({
                    'title': title,
                    'date': event_date,
                    'url': url,
                    'time_from': time_from,
                    'venue': venue,
                    'city': city,
                    'country_code': 'US',
                    'description': description,
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                })

        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    OrchestraMiamiOrgCrawler().run()


if __name__ == '__main__':
    main()
