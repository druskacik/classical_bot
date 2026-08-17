import html
import re
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://worcesterchambermusic.org/'
SOURCE = 'Worcester Chamber Music Society'
API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/pages')
HEADERS = {'User-Agent': 'classical-concert-crawler/1.0 (+https://classicalbot.com/)'}
MONTHS = {
    name: number for number, name in enumerate(
        ('January', 'February', 'March', 'April', 'May', 'June', 'July',
         'August', 'September', 'October', 'November', 'December'), 1
    )
}
DATE_RE = re.compile(
    r'(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(?P<month>' + '|'.join(MONTHS) + r')\s+(?P<day>\d{1,2})'
    r'(?:,?\s+(?P<year>20\d{2}))?\s*[@–-]?\s*'
    r'(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[AP]M)',
    re.IGNORECASE,
)

VENUE_CITIES = {
    'jeanne y. curtis performance hall': 'Worcester',
    'razzo hall': 'Worcester',
    'traina center for the arts': 'Worcester',
    'st. peter’s catholic church': 'Worcester',
    'assumption university': 'Worcester',
    'mechanics hall': 'Worcester',
    'museum of worcester': 'Worcester',
    'american antiquarian society': 'Worcester',
    'fitchburg art museum': 'Fitchburg',
    'icon museum and study center': 'Clinton',
    'first congregational church': 'Princeton',
    'unitarian universalist church': 'Harvard',
}


def clean_text(value):
    value = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    value = html.unescape(value).replace('\xa0', ' ')
    return re.sub(r'[ \t]+', ' ', value).strip()


def season_years(title):
    match = re.search(r'\b(20\d{2})\s*[-–]\s*(?:(20)?(\d{2}))\b', title)
    if not match:
        match = re.search(r'\b(20\d{2})\s*[-–]\s*(20\d{2})\b', title)
    if not match:
        return None
    start = int(match.group(1))
    end = (start // 100) * 100 + int(match.group(3))
    return start, end


def event_year(month, years):
    return years[0] if MONTHS[month.title()] >= 7 else years[1]


def parse_date_time(match, years):
    year = int(match.group('year')) if match.group('year') else event_year(match.group('month'), years)
    try:
        event_date = date(year, MONTHS[match.group('month').title()], int(match.group('day'))).isoformat()
    except ValueError:
        return None, None
    hour = int(match.group('hour')) % 12
    if match.group('ampm').upper() == 'PM':
        hour += 12
    return event_date, f"{hour:02d}:{int(match.group('minute') or 0):02d}"


def venue_and_city(lines, date_index):
    candidates = []
    for line in lines[date_index + 1:date_index + 4]:
        line = line.strip(' ,')
        if not line or DATE_RE.search(line) or re.match(r'^(Works by|With |Featured work|We |Our |A )', line, re.I):
            break
        candidates.append(line)
    joined = ', '.join(candidates)
    for clue, city in VENUE_CITIES.items():
        if clue in joined.lower():
            venue = next((item for item in candidates if clue in item.lower()), candidates[0])
            venue = re.sub(rf',\s*{re.escape(city)}$', '', venue, flags=re.I)
            return venue, city
    if candidates and ',' in candidates[0]:
        venue, city = (part.strip() for part in candidates[0].rsplit(',', 1))
        if venue and city and len(city.split()) <= 3:
            return venue, city
    return None, None


def detail_description(session, url):
    try:
        response = session.get(API_URL, params={'slug': urlparse(url).path.strip('/'), '_fields': 'content'}, timeout=30)
        response.raise_for_status()
        pages = response.json()
        if pages:
            soup = BeautifulSoup(pages[0]['content']['rendered'], 'html.parser')
            text = clean_text(soup)
            return re.sub(r'\[[^\]]+\]', ' ', text).strip() or None
    except (requests.RequestException, ValueError, KeyError) as error:
        log_message('Failed to fetch event detail', event='crawler_detail_fetch_failed', level='warning',
                    url=url, error_type=type(error).__name__, error_message=str(error))
    return None


def parse_schedule(session, page, years):
    response = session.get(page['link'], timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    records = []
    for heading in soup.select('h2.vc_custom_heading'):
        row = heading.find_parent('div', class_='vc_row')
        title = re.sub(r'\s+', ' ', clean_text(heading))
        if not row or title == clean_text(soup.select_one('h1.entry-title') or ''):
            continue
        lines = [line.strip() for line in clean_text(row).splitlines() if line.strip()]
        detail_url = next((urljoin(page['link'], a['href']) for a in row.find_all('a', href=True)
                           if urlparse(urljoin(page['link'], a['href'])).netloc == urlparse(SOURCE_URL).netloc
                           and 'learn more' in clean_text(a).lower()), page['link'])
        description = detail_description(session, detail_url)
        for index, line in enumerate(lines):
            match = DATE_RE.search(line)
            if not match:
                continue
            event_date, time_from = parse_date_time(match, years)
            venue, city = venue_and_city(lines, index)
            if not all((event_date, venue, city)):
                continue
            records.append({
                'title': title, 'date': event_date, 'url': detail_url,
                'time_from': time_from, 'venue': venue, 'city': city,
                'country_code': 'US', 'description': description,
                'source_url': SOURCE_URL, 'source': SOURCE,
            })
    return records


class WorcesterChamberMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='worcesterchambermusic_org', source=SOURCE, source_url=SOURCE_URL,
        country_code='US', upload_target='classical',
        columns=['title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
                 'description', 'source_url', 'source'],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(API_URL, params={'search': 'concert schedule', 'per_page': 100,
                                                    '_fields': 'link,title'}, timeout=45)
            response.raise_for_status()
            pages = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message('Failed to discover concert schedules', event='crawler_fetch_failed', level='error',
                        url=API_URL, error_type=type(error).__name__, error_message=str(error))
            raise
        records = []
        for page in pages:
            title = BeautifulSoup(page['title']['rendered'], 'html.parser').get_text(' ', strip=True)
            years = season_years(title)
            if not years or not re.search(r'concert(?:s| schedule)$', title, re.I):
                continue
            try:
                records.extend(parse_schedule(session, page, years))
            except requests.RequestException as error:
                log_message('Failed to fetch concert schedule', event='crawler_schedule_fetch_failed',
                            level='warning', url=page['link'], error_type=type(error).__name__,
                            error_message=str(error))
        return records


def main():
    return WorcesterChamberMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
