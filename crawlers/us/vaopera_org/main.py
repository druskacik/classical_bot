import html
import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://vaopera.org/'
SOURCE = 'Virginia Opera'
API_URL = 'https://vaopera.org/wp-json/wp/v2/pages'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://vaopera.org/events/',
    'Sec-Fetch-Site': 'same-origin',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Dest': 'empty',
    'sec-ch-ua': '"Chromium";v="151", "Not=A?Brand";v="99"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Linux"',
}

# Virginia Opera's production pages identify occurrences by city. These are
# the company's three first-party venue pages and the stable venue mappings
# used by its production calendar.
VENUES = {
    'Norfolk': 'Harrison Opera House',
    'Richmond': 'Carpenter Theatre at Dominion Energy Center',
    'Fairfax': 'Center for the Arts at George Mason University',
}

OCCURRENCE_RE = re.compile(
    r'(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})\s*@\s*'
    r'(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>AM|PM)',
    re.IGNORECASE,
)
COMMUNITY_RE = re.compile(
    r'^(?P<title>[^|]+)\|\s*'
    r'(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s*'
    r'(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>AM|PM)\s*\|\s*'
    r'(?P<venue>[^|]+)',
    re.IGNORECASE,
)

COMMUNITY_VENUE_CITIES = {
    'Harrison Opera House': 'Norfolk',
    'Richmond Public Library': 'Richmond',
    'Williamsburg Public Library': 'Williamsburg',
    'Lemon Tree Gallery': 'Cape Charles',
    'Senior Resource Center': 'Virginia Beach',
    'Christopher Newport University': 'Newport News',
    'Williamsburg Regional Library': 'Williamsburg',
    'Stockley Gardens Fall Art Show': 'Norfolk',
    'TCC Joint-Use Library': 'Virginia Beach',
    'Windsor Woods Library': 'Virginia Beach',
    'Meyera E. Oberndorf Central Library': 'Virginia Beach',
}


def clean_text(value):
    if not value:
        return ''
    value = (
        html.unescape(str(value))
        .replace('\xa0', ' ')
        .replace('\u200b', '')
        .replace('\ufeff', '')
    )
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def event_year(match, modified):
    """Resolve the omitted year using the page's near-performance edit date."""
    month = datetime.strptime(match.group('month')[:3], '%b').month
    day = int(match.group('day'))
    candidates = []
    earliest = modified.date().toordinal() - 45
    for year in range(modified.year - 1, modified.year + 3):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        # Pages are normally finalized shortly before opening, though a few
        # receive small corrections just after opening. Avoid relying on the
        # printed weekday because occasional first-party typos occur there.
        if candidate.toordinal() >= earliest:
            distance = abs((candidate - modified.date()).days)
            candidates.append((distance, year))
    return min(candidates)[1] if candidates else None


def parse_time(match):
    hour = int(match.group('hour')) % 12
    if match.group('ampm').upper() == 'PM':
        hour += 12
    minute = int(match.group('minute') or 0)
    return f'{hour:02d}:{minute:02d}'


def page_description(soup):
    parts = []
    for node in soup.find_all(['p', 'h3']):
        text = clean_text(node.get_text(' ', strip=True))
        if not text or '[et_pb_' in text or OCCURRENCE_RE.search(text):
            continue
        if text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_page(page):
    content = html.unescape(page.get('content', {}).get('rendered', ''))
    soup = BeautifulSoup(content, 'html.parser')
    title_node = soup.find('h1')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    url = page.get('link', '').strip()
    try:
        modified = datetime.fromisoformat(page['modified'])
    except (KeyError, TypeError, ValueError):
        return []
    if not title or not url:
        return []

    description = page_description(soup)
    records = []
    for heading in soup.find_all('h2'):
        city = clean_text(heading.get_text(' ', strip=True))
        venue = VENUES.get(city)
        if not venue:
            continue
        schedule = heading.find_next('p')
        if not schedule:
            continue
        schedule_text = clean_text(schedule.get_text(' ', strip=True))
        for match in OCCURRENCE_RE.finditer(schedule_text):
            year = event_year(match, modified)
            if year is None:
                continue
            month = datetime.strptime(match.group('month')[:3], '%b').month
            try:
                event_date = date(year, month, int(match.group('day'))).isoformat()
            except ValueError:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': parse_time(match),
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def parse_community_events(page):
    """Parse concrete standalone listings embedded in the all-events page."""
    content = html.unescape(page.get('content', {}).get('rendered', ''))
    soup = BeautifulSoup(content, 'html.parser')
    try:
        modified = datetime.fromisoformat(page['modified'])
    except (KeyError, TypeError, ValueError):
        return []

    records = []
    for paragraph in soup.find_all('p'):
        text = clean_text(paragraph.get_text(' | ', strip=True))
        match = COMMUNITY_RE.search(text)
        if not match:
            continue
        venue_text = clean_text(match.group('venue'))
        city = None
        venue = venue_text
        if ',' in venue_text:
            possible_venue, possible_city = venue_text.rsplit(',', 1)
            if possible_city.strip() in set(COMMUNITY_VENUE_CITIES.values()):
                venue, city = possible_venue.strip(), possible_city.strip()
        if city is None:
            city = COMMUNITY_VENUE_CITIES.get(venue)
        if not city or not venue:
            continue
        year = event_year(match, modified)
        month = datetime.strptime(match.group('month')[:3], '%b').month
        if year is None:
            continue
        try:
            event_date = date(year, month, int(match.group('day'))).isoformat()
        except ValueError:
            continue
        records.append({
            'title': clean_text(match.group('title')),
            'date': event_date,
            'url': page.get('link', SOURCE_URL) + '#lectures',
            'time_from': parse_time(match),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': text,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def parse_recurring_events(page, page_urls):
    """Parse month/day lists used by recurring special-event cards."""
    content = html.unescape(page.get('content', {}).get('rendered', ''))
    soup = BeautifulSoup(content, 'html.parser')
    modified = datetime.fromisoformat(page['modified'])
    records = []
    for heading in soup.find_all('h5'):
        title = clean_text(heading.get_text(' ', strip=True))
        schedule_node = heading.find_next('p')
        if not schedule_node:
            continue
        schedule = clean_text(schedule_node.get_text(' | ', strip=True))
        parts = [clean_text(part) for part in schedule.split('|')]
        if len(parts) < 3 or not re.match(r'^[A-Z][a-z]{2}\.', parts[0]):
            continue
        time_match = re.fullmatch(
            r'(\d{1,2})(?::(\d{2}))?\s*(AM|PM)', parts[1], re.IGNORECASE
        )
        venue_text = parts[2]
        if not time_match or ',' not in venue_text:
            continue
        venue, city = [clean_text(value) for value in venue_text.rsplit(',', 1)]
        if not venue or not city:
            continue
        hour = int(time_match.group(1)) % 12
        if time_match.group(3).upper() == 'PM':
            hour += 12
        time_from = f'{hour:02d}:{int(time_match.group(2) or 0):02d}'

        current_month = None
        for token in [clean_text(value) for value in parts[0].split(',')]:
            month_match = re.match(r'^([A-Z][a-z]{2})\.\s*(\d{1,2})$', token)
            if month_match:
                current_month = datetime.strptime(month_match.group(1), '%b').month
                day = int(month_match.group(2))
            elif current_month and token.isdigit():
                day = int(token)
            else:
                continue
            candidates = [
                date(year, current_month, day)
                for year in range(modified.year, modified.year + 3)
                if date(year, current_month, day).toordinal()
                >= modified.date().toordinal() - 45
            ]
            if not candidates:
                continue
            event_date = min(
                candidates, key=lambda value: abs((value - modified.date()).days)
            ).isoformat()
            records.append({
                'title': title,
                'date': event_date,
                'url': page_urls.get(title.lower(), page.get('link', SOURCE_URL)),
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': schedule,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def get_pages(session):
    response = session.get(
        API_URL,
        params={
            'per_page': 100,
            '_fields': 'link,slug,title,content,modified',
        },
        timeout=90,
    )
    response.raise_for_status()
    return response.json()


class VaoperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='vaopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        pages = get_pages(session)
        page_urls = {
            clean_text(page.get('title', {}).get('rendered', '')).lower(): page.get('link')
            for page in pages
        }
        records = []
        for page in pages:
            try:
                records.extend(parse_page(page))
                if page.get('slug') == 'events':
                    records.extend(parse_community_events(page))
                    records.extend(parse_recurring_events(page, page_urls))
            except (AttributeError, TypeError, ValueError) as error:
                log_message(
                    'Failed to parse Virginia Opera page',
                    event='crawler_item_failed',
                    level='warning',
                    url=page.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    VaoperaOrgCrawler().run()


if __name__ == '__main__':
    main()
