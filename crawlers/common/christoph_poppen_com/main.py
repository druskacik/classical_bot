import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://christoph-poppen.com/'
CALENDAR_URLS = (
    urljoin(SOURCE_URL, 'concert-event-calendar/'),
    urljoin(SOURCE_URL, 'archive-concerts/'),
)
SOURCE = 'Christoph Poppen'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en,de;q=0.9',
}

CITY_COUNTRIES = {
    'Athens': 'GR', 'Augsburg': 'DE', 'Beijing': 'CN', 'Cologne': 'DE',
    'Düsseldorf': 'DE', 'Hong Kong': 'HK', 'Huglfing': 'DE',
    'Jerusalem': 'IL', 'Madrid': 'ES', 'Marvão': 'PT', 'Padova': 'IT',
    'Seoul': 'KR', 'Stuttgart': 'DE', 'Tel Aviv': 'IL', 'Vienna': 'AT',
}

CITY_ALIASES = {
    'Athen': 'Athens', 'Honkong': 'Hong Kong', 'HongKong': 'Hong Kong',
    'Hongkong': 'Hong Kong', 'Köln': 'Cologne', 'Wien': 'Vienna',
}

DATE_RE = re.compile(r'(?<!\d)(\d{1,2})\.(\d{1,2})\.(20\d{2})(?!\d)')
TIME_RE = re.compile(r'(?<!\d)(\d{1,2})(?:[h:.])(\d{2})(?!\d)', re.I)
MONTHS = {
    'JAN': 1, 'FEB': 2, 'MARCH': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUNE': 6, 'JUL': 7, 'JULY': 7, 'AUG': 8, 'AUGUST': 8,
    'SEPT': 9, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_city(text):
    searchable = re.sub(r'\s+', ' ', text)
    candidates = list(CITY_COUNTRIES) + list(CITY_ALIASES)
    for candidate in sorted(candidates, key=len, reverse=True):
        if re.search(rf'(?<!\w){re.escape(candidate)}(?!\w)', searchable, re.I):
            for alias, city in CITY_ALIASES.items():
                if candidate.lower() == alias.lower():
                    return city
            for city in CITY_COUNTRIES:
                if candidate.lower() == city.lower():
                    return city
    return ''


def parse_date_time(text):
    match = DATE_RE.search(text)
    if not match:
        return None, None
    try:
        event_date = date(int(match.group(3)), int(match.group(2)), int(match.group(1))).isoformat()
    except ValueError:
        return None, None
    time_match = TIME_RE.search(text[match.end():])
    if not time_match:
        return event_date, None
    hour, minute = int(time_match.group(1)), int(time_match.group(2))
    if hour > 23 or minute > 59:
        return event_date, None
    return event_date, f'{hour:02d}:{minute:02d}'


def date_line(soup):
    content = soup.select_one('.qodef-m-section-content') or soup.select_one('main')
    if not content:
        return ''
    for node in content.find_all(string=DATE_RE):
        parent = node.parent
        if parent:
            return clean_text(parent)
    return ''


def infer_venue(line, title, city):
    remainder = DATE_RE.sub('', line, count=1).strip(' –-')
    parts = [part.strip() for part in re.split(r'\s+[–—]\s+', remainder) if part.strip()]
    parts = [part for part in parts if not TIME_RE.fullmatch(part)]
    location = parts[-1] if parts else ''
    city_location = re.search(
        rf'(?:{re.escape(city)}|{re.escape(next((a for a, c in CITY_ALIASES.items() if c == city), city))})\s*,\s*([^\n–—]+)',
        remainder,
        re.I,
    ) if city else None
    if city_location:
        location = city_location.group(1).strip()
    elif '\n' in location:
        location = location.splitlines()[-1].strip()
    location = re.sub(r'^(?:at\s+)', '', location, flags=re.I)
    location = re.sub(r'\s+[–—]\s+.*$', '', location)
    location = re.sub(r'\s*[|,]\s*(?:Via|Street|Stra(?:ss|ß)e)\b.*$', '', location, flags=re.I)
    location = re.sub(r'\s*\([^)]*\b(?:Calle|Street|Stra(?:ss|ß)e|Via)\b[^)]*\)', '', location, flags=re.I)
    location = re.sub(r',\s*(?:Athens|Athen|Cologne|Köln|Madrid|Padova|Vienna|Wien)\s*$', '', location, flags=re.I)
    if city and re.match(rf'^{re.escape(city)}\s*,\s*', location, re.I):
        location = re.sub(rf'^{re.escape(city)}\s*,\s*', '', location, flags=re.I)
    for alias, canonical in CITY_ALIASES.items():
        if canonical == city:
            location = re.sub(rf'^{re.escape(alias)}\s*,\s*', '', location, flags=re.I)
    if location and canonical_city(location) == city and not re.search(
        r'hall|museum|auditor|philharm|musikverein|kapelle|church|academy|school', location, re.I
    ):
        location = ''
    if not location:
        title_parts = [part.strip() for part in title.split('|')]
        if title_parts:
            candidate = title_parts[-1]
            if canonical_city(candidate) != city or re.search(
                r'hall|museum|auditor|philharm|musikverein|kapelle|church', candidate, re.I
            ):
                location = candidate
    return location.strip(' ,–-')


def parse_event(html, url, listing_text):
    soup = BeautifulSoup(html, 'html.parser')
    heading = soup.select_one('main h1, main .qodef-m-title')
    title = clean_text(heading)
    line = date_line(soup)
    event_date, time_from = parse_date_time(line)
    title_date = re.match(r'\s*(\d{1,2})\s+([A-Z]+)\b', title.upper())
    if event_date and title_date and title_date.group(2) in MONTHS:
        try:
            event_date = date(
                int(event_date[:4]), MONTHS[title_date.group(2)], int(title_date.group(1))
            ).isoformat()
        except ValueError:
            pass
    detail_text = clean_text(soup.select_one('.qodef-m-section-content') or soup.select_one('main'))
    city = canonical_city(f'{listing_text}\n{line}\n{title}')
    country_code = CITY_COUNTRIES.get(city)
    venue = infer_venue(line, title, city)
    if not all((title, event_date, url, venue, city, country_code)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': detail_text or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event(item):
    url, listing_text = item
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event(response.text, url, listing_text)


class ChristophPoppenComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='christoph_poppen_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        events = {}
        for calendar_url in CALENDAR_URLS:
            response = requests.get(calendar_url, headers=HEADERS, timeout=45)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            for article in soup.select('article'):
                link = article.select_one('a[href*="/concert-agenda/"]')
                if link and link.get('href'):
                    url = urljoin(SOURCE_URL, link['href']).split('#', 1)[0]
                    events[url] = clean_text(article)

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_event, item): item[0] for item in events.items()}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Christoph Poppen event detail',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Christoph Poppen event',
                        event='crawler_item_skipped', level='warning', url=url,
                        error_type='IncompleteEventData',
                        error_message='Required date, title, venue, city, or country is missing',
                    )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    ChristophPoppenComCrawler().run()


if __name__ == '__main__':
    main()
