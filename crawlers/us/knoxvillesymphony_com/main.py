import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://knoxvillesymphony.com/'
CALENDAR_URL = f'{SOURCE_URL}concert-calendar/'
FILTER_URL = f'{SOURCE_URL}wp-content/themes/kso/ajax-concert.php'
SOURCE = 'Knoxville Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTH_PATTERN = (
    r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
)
DATE_TIME_RE = re.compile(
    rf'\b({MONTH_PATTERN})\s+(\d{{1,2}})(?:,?\s+(\d{{4}}))?'
    r'(?:,?\s+at\s+(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?))?',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_time(value):
    value = re.sub(r'\.', '', clean_text(value)).upper()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_occurrences(values, fallback_year):
    occurrences = []
    explicit_years = []
    for value in values:
        explicit_years.extend(
            match.group(3) for match in DATE_TIME_RE.finditer(clean_text(value))
            if match.group(3)
        )
    inferred_year = explicit_years[0] if len(set(explicit_years)) == 1 else fallback_year
    for value in values:
        for month, day, explicit_year, event_time in DATE_TIME_RE.findall(clean_text(value)):
            try:
                event_date = datetime.strptime(
                    f'{month} {day} {explicit_year or inferred_year}', '%B %d %Y'
                ).date().isoformat()
            except ValueError:
                try:
                    event_date = datetime.strptime(
                        f'{month[:3]} {day} {explicit_year or inferred_year}', '%b %d %Y'
                    ).date().isoformat()
                except ValueError:
                    continue
            occurrence = (event_date, parse_time(event_time))
            if occurrence not in occurrences:
                occurrences.append(occurrence)
    return occurrences


def fetch_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def venue_city(session, venue_url):
    if not venue_url:
        return None
    soup = fetch_soup(session, venue_url)
    candidates = [clean_text(node) for node in soup.select('.wrapper h4, meta[property="og:description"]')]
    for node in soup.select('meta[property="og:description"]'):
        candidates.append(clean_text(node.get('content')))
    state_patterns = (
        re.compile(r',\s*([^,]+),\s*(?:TN|Tennessee)\b', re.I),
        re.compile(r',\s*([A-Za-z .\'-]+?)\s+(?:TN|Tennessee)\b', re.I),
    )
    for candidate in candidates:
        for state_pattern in state_patterns:
            match = state_pattern.search(candidate)
            if match:
                city = match.group(1).strip(' ,')
                if city:
                    return city
    return None


def detail_records(url, fallback_year):
    session = requests.Session()
    session.headers.update(HEADERS)
    soup = fetch_soup(session, url)
    section = soup.select_one('.inner-performances')
    if not section:
        return []

    title = clean_text(section.select_one('.title h2'))
    venue_link = section.select_one('.left-box a[href*="/venue/"]')
    venue = clean_text(venue_link)
    venue_url = venue_link.get('href') if venue_link else None
    series = clean_text(section.select_one('.title h4'))
    city = venue_city(session, venue_url)
    if not city and 'road' not in series.lower():
        city = 'Knoxville'
    if not title or not venue or not city:
        return []

    performance_values = [
        node.get('data-ticket-time') or clean_text(node)
        for node in section.select('[data-ticket-time]')
    ]
    performance_values.extend(
        clean_text(node) for node in section.select('.left-box .text-box > p')
    )
    occurrences = parse_occurrences(performance_values, fallback_year)
    if not occurrences:
        occurrences = parse_occurrences(
            [clean_text(section.select_one('.title h3'))], fallback_year
        )
    occurrences = [item for item in occurrences if item[0].startswith(f'{fallback_year}-')]

    description_parts = []
    for node in soup.select('.inner-performances .full-box .text-box, .get-tickets .text-box'):
        text = node.get_text('\n', strip=True)
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r' *\n *', '\n', text).strip()
        if text and text not in description_parts:
            description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    return [
        {
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
        }
        for event_date, time_from in occurrences
    ]


def calendar_entries(session):
    # The live calendar retains events back to 2025. Querying by calendar year
    # supplies the year omitted from its rendered event cards.
    current_year = date.today().year
    entries = set()
    for year in range(2025, current_year + 3):
        response = session.post(
            FILTER_URL,
            data={
                'type': 'all',
                'start': f'{year}-01-01',
                'end': f'{year}-12-31',
            },
            timeout=45,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        for link in soup.select('.item-c a[href*="/concert/"]'):
            url = link.get('href')
            if url:
                entries.add((url, year))
    return sorted(entries)


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    entries = calendar_entries(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(detail_records, url, year): url for url, year in entries
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Concert detail request failed',
                    event='crawler_detail_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No parseable concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class KnoxvilleSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='knoxvillesymphony_com',
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
        return scrape_concerts()


def main():
    KnoxvilleSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
