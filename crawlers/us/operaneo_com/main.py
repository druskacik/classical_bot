import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operaneo.com/'
SOURCE = 'Opera Neo'
SEASON_PATHS = ('2022-season', '2023-season', '2024-season', '2025', '2026')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}
MONTHS = {
    name: number for number, name in enumerate(
        ('January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December'),
        1,
    )
}
DATE_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2})(?:\s*,?\s*(20\d{2}))?\s+(?:at|@)\s+'
    r'(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?m\.?',
    re.IGNORECASE,
)
SECOND_DAY_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'(\d{1,2})\s+(?:at|@)\s+(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?m\.?',
    re.IGNORECASE,
)
NON_EVENT_PATH_PARTS = (
    'photo', 'rehearsal', 'bar-menu', 'audition', 'donor', 'media', 'about',
    'board', 'contact', 'episode', 'how-to-watch', 'buyaccess', 'sign-up',
)
EXCLUDED_PATHS = {'aria-gala-2024'}  # Stale duplicate of the 2024 Cabaret page.


def clean_text(value):
    return re.sub(r'\s+', ' ', value or '').strip()


def visible_text(soup):
    copy = BeautifulSoup(str(soup), 'html.parser')
    for node in copy(['script', 'style', 'noscript', 'svg']):
        node.decompose()
    return clean_text(copy.get_text(' ', strip=True))


def page_title(soup):
    node = soup.select_one('meta[property="og:title"]')
    title = node.get('content', '') if node else ''
    if not title and soup.title:
        title = soup.title.get_text(' ', strip=True)
    return clean_text(re.sub(r'\s*[|–-]\s*Opera Neo\s*$', '', title, flags=re.IGNORECASE))


def parse_time(hour, minute, meridiem):
    hour = int(hour) % 12
    if meridiem.lower() == 'p':
        hour += 12
    return f'{hour:02d}:{minute or "00"}'


def valid_date(year, month, day):
    try:
        return date(int(year), MONTHS[month.title()], int(day)).isoformat()
    except (KeyError, TypeError, ValueError):
        return None


def extract_venue_and_city(text, first_date_start):
    prefix = text[:first_date_start]
    matches = list(re.finditer(r'\bPerformances?\s+at\s+(?:the\s+)?', prefix, re.IGNORECASE))
    segment = prefix[matches[-1].end():] if matches else prefix[prefix.rfind('DONATE') + 6:]
    address = re.search(
        r'\b\d{2,5}\s+.{2,100}?,\s*(San Diego|La Jolla)\s*,?\s*(?:C\s*A\s*)?\d{5}\b',
        segment,
        re.IGNORECASE,
    )
    if address:
        venue = segment[:address.start()]
        if not matches and ':' in venue:
            venue = venue.rsplit(':', 1)[-1]
        city = address.group(1)
    else:
        # Some archived pages give a venue but no street address.
        weekday = re.search(
            r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s*$',
            segment,
            re.IGNORECASE,
        )
        venue = segment[:weekday.start()] if weekday else segment
        city = 'San Diego'
    venue = clean_text(venue).replace('Q ualcomm', 'Qualcomm').strip(' |,')
    return (venue or None), city


def parse_event_page(url, html, year):
    soup = BeautifulSoup(html, 'html.parser')
    text = visible_text(soup)
    matches = list(DATE_RE.finditer(text))
    if not matches:
        return []
    venue, city = extract_venue_and_city(text, matches[0].start())
    title = page_title(soup)
    if not title or not venue or not city:
        return []

    records = []
    seen = set()
    for match in matches:
        event_date = valid_date(match.group(3) or year, match.group(1), match.group(2))
        time_from = parse_time(match.group(4), match.group(5), match.group(6))
        if event_date:
            seen.add((event_date, time_from))
        # Opera Neo commonly writes the second occurrence as "Saturday, 16 at 7:30pm".
        tail = text[match.end():match.end() + 80]
        second = SECOND_DAY_RE.search(tail)
        if second:
            second_date = valid_date(match.group(3) or year, match.group(1), second.group(1))
            second_time = parse_time(second.group(2), second.group(3), second.group(4))
            if second_date:
                seen.add((second_date, second_time))

    description = text[:re.search(r'\bAdministrative Address\b', text, re.IGNORECASE).start()] \
        if re.search(r'\bAdministrative Address\b', text, re.IGNORECASE) else text
    for event_date, time_from in sorted(seen):
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def season_links(html):
    soup = BeautifulSoup(html, 'html.parser')
    links = set()
    for anchor in soup.find_all('a', href=True):
        url = urljoin(SOURCE_URL, anchor['href']).split('#', 1)[0]
        parsed = urlparse(url)
        path = parsed.path.strip('/')
        if parsed.netloc.lower() not in ('operaneo.com', 'www.operaneo.com') or not path:
            continue
        if (path in SEASON_PATHS or path in EXCLUDED_PATHS
                or any(part in path.lower() for part in NON_EVENT_PATH_PARTS)):
            continue
        links.add(f'{SOURCE_URL.rstrip("/")}/{path}')
    return links


def parse_2022_overview(html):
    """The two 2022 one-off concerts link to the homepage, so retain them here."""
    text = visible_text(BeautifulSoup(html, 'html.parser'))
    specs = (
        ('Aria Gala', 'July', 10, '18:00', 'The Conrad Prebys Performing Arts Center'),
        ('Cabaret', 'July', 22, '19:30', 'Bread & Salt'),
        ('Cabaret', 'July', 23, '19:30', 'Bread & Salt'),
    )
    records = []
    for title, month, day, time_from, venue in specs:
        event_date = valid_date(2022, month, day)
        if event_date and (re.search(rf'{month}\s+{day}\b', text, re.IGNORECASE)
                           or (day == 23 and re.search(r'July\s+22\s*&\s*23\b', text, re.IGNORECASE))):
            records.append({
                'title': title, 'date': event_date, 'url': f'{SOURCE_URL}2022-season',
                'time_from': time_from, 'venue': venue, 'city': 'San Diego',
                'country_code': 'US', 'description': text, 'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class OperaneoComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operaneo_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        season_pages = {}
        try:
            for path in SEASON_PATHS:
                response = session.get(urljoin(SOURCE_URL, path), timeout=60)
                response.raise_for_status()
                season_pages[path] = response.text
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Opera Neo season page', event='crawler_fetch_failed',
                level='error', url=response.url if 'response' in locals() else SOURCE_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        url_year = {}
        for path, html in season_pages.items():
            year_match = re.search(r'20\d{2}', path)
            year = int(year_match.group()) if year_match else 2025
            for url in season_links(html):
                url_year[url] = max(year, url_year.get(url, 0))
        # This concrete 2024 gala remains published and indexed, but its season tile
        # points at a stale duplicate path instead of the gala detail page.
        url_year[f'{SOURCE_URL}gala-2024'] = 2024

        records = parse_2022_overview(season_pages['2022-season'])
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(session.get, url, timeout=60): (url, year)
                for url, year in url_year.items()
            }
            for future in as_completed(futures):
                url, year = futures[future]
                try:
                    response = future.result()
                    response.raise_for_status()
                    records.extend(parse_event_page(url, response.text, year))
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Opera Neo event page', event='crawler_fetch_failed',
                        level='warning', url=url, error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return records


def main():
    return OperaneoComCrawler().run()


if __name__ == '__main__':
    main()
