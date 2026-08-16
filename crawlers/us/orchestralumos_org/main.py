import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orchestralumos.org/'
SOURCE = 'Orchestra Lumos'
COMMUNITY_URLS = (
    urljoin(SOURCE_URL, 'community-events-calendar/'),
    urljoin(SOURCE_URL, 'past-community-events/'),
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}
DATE_RE = re.compile(
    r'(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(?P<month>[A-Z][a-z]+)\s+(?P<day>\d{1,2}),\s+(?P<year>20\d{2})'
    r'\s+at\s+(?P<time>\d{1,2}(?::\d{2})?\s*[ap]m)',
    re.I,
)
CITY_RE = re.compile(
    r'\b(Old Greenwich|Greenwich|New Canaan|Westport|Norwalk|Darien|Stamford|Fairfield|Rye)\b',
    re.I,
)
STATE_RE = re.compile(r'\b(CT|NY)\b', re.I)
ACTION_TEXT = re.compile(r'^(learn more|more details|purchase tickets)$', re.I)


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = str(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def parse_date(match):
    try:
        return datetime.strptime(
            f"{match.group('month')} {match.group('day')} {match.group('year')}",
            '%B %d %Y',
        ).date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    value = re.sub(r'(?<=\d)(?=[AP]M\b)', ' ', value.strip().upper())
    value = re.sub(r'\s+', ' ', value)
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def city_from_text(value):
    match = CITY_RE.search(value or '')
    return match.group(1).title() if match else None


def venue_from_location(value, city):
    if not value:
        return None
    value = clean_text(value).strip(' .,-')
    value = re.split(r'\b(?:registration|unfortunately|non-members|please call)\b', value, 1, flags=re.I)[0]
    value = re.sub(r'\b\d{1,6}\s+[A-Za-z0-9].*$', '', value).strip(' ,.-')
    value = re.sub(r',?\s*(?:CT|NY)(?:\s+\d{5})?$', '', value, flags=re.I).strip(' ,.-')
    if city:
        value = re.sub(rf',\s*{re.escape(city)}$', '', value, flags=re.I).strip(' ,.-')
    return value or None


def soup_for(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def find_season_url(home_soup):
    candidates = []
    for link in home_soup.select('a[href]'):
        text = clean_text(link).lower()
        href = urljoin(SOURCE_URL, link.get('href'))
        if urlparse(href).netloc == urlparse(SOURCE_URL).netloc and 'season' in text and 'concert' in text:
            candidates.append(href)
    return candidates[0] if candidates else None


def detail_links(season_soup, season_url):
    links = []
    for row in season_soup.select('.wpb_row.first-row'):
        if not DATE_RE.search(clean_text(row)):
            continue
        link = row.find('a', href=True, string=lambda value: value and ACTION_TEXT.match(clean_text(value)))
        if link:
            links.append(urljoin(season_url, link['href']))
    return list(dict.fromkeys(links))


def detail_locations(soup):
    for heading in soup.select('h2, h3, h4, h5'):
        if clean_text(heading).lower() == 'location':
            block = heading.parent
            lines = [line for line in clean_text(block).splitlines() if line.lower() != 'location']
            if not lines:
                continue
            locations = []
            for index, line in enumerate(lines):
                if re.search(r'performance at$', line, re.I) and index + 2 < len(lines):
                    city = city_from_text(lines[index + 2])
                    locations.append((venue_from_location(lines[index + 1], city), city))
            if locations:
                return locations
            city = city_from_text(' '.join(lines))
            return [(venue_from_location(lines[0], city), city)]
    return []


def parse_detail(soup, url):
    main = soup.select_one('main') or soup.select_one('#ajax-content-wrap') or soup
    text = clean_text(main)
    title_node = main.find('h1')
    title = clean_text(title_node).replace('\n', ' - ')
    if not title:
        return []

    locations = detail_locations(soup)
    records = []
    seen = set()
    location_by_date = {}
    for match in DATE_RE.finditer(text):
        event_date = parse_date(match)
        time_from = parse_time(match.group('time'))
        context = text[match.end(): match.end() + 140].split('\n', 1)[0]
        explicit = re.match(r'\s+at\s+(.+?)\s+in\s+([A-Za-z ]+)', context, re.I)
        if event_date not in location_by_date and locations:
            location_by_date[event_date] = locations[min(len(location_by_date), len(locations) - 1)]
        location = location_by_date.get(event_date, (None, None))
        venue = clean_text(explicit.group(1)) if explicit else location[0]
        city = clean_text(explicit.group(2)) if explicit else location[1]
        if not city:
            city = city_from_text(context)
        key = (event_date, time_from, venue, city)
        if event_date and venue and city and key not in seen:
            seen.add(key)
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': text,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def parse_community_row(row, page_url):
    text = clean_text(row)
    headings = [clean_text(node) for node in row.select('h2, h4')]
    h2 = row.find('h2')
    title = clean_text(h2)
    if not title:
        return []

    reading_rhythm = any('reading and rhythm' in item.lower() for item in headings)
    if reading_rhythm:
        venue = title
        title = 'Reading and Rhythm'
    else:
        venue = None

    description = text or None
    records = []
    for match in DATE_RE.finditer(text):
        event_date = parse_date(match)
        tail = clean_text(text[match.end(): match.end() + 280]).replace('\n', ' ').lstrip(' .,-')
        before = text[max(0, match.start() - 160):match.start()]
        city = city_from_text(tail) or city_from_text(before)
        if reading_rhythm:
            city = city_from_text(' '.join(headings)) or city
        else:
            venue = venue_from_location(tail, city)
        if event_date and city and venue:
            records.append({
                'title': title,
                'date': event_date,
                'url': page_url,
                'time_from': parse_time(match.group('time')),
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []

    home_soup = soup_for(session, SOURCE_URL)
    season_url = find_season_url(home_soup)
    if season_url:
        season_soup = soup_for(session, season_url)
        for url in detail_links(season_soup, season_url):
            try:
                records.extend(parse_detail(soup_for(session, url), url))
            except requests.RequestException as error:
                log_message(
                    'Concert detail request failed',
                    event='crawler_detail_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    else:
        log_message(
            'No season concert page found',
            event='crawler_season_missing',
            level='warning',
            url=SOURCE_URL,
        )

    for page_url in COMMUNITY_URLS:
        soup = soup_for(session, page_url)
        for row in soup.select('.wpb_row.first-row'):
            records.extend(parse_community_row(row, page_url))

    records = list({
        (item['title'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }.values())
    records.sort(key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']))
    if not records:
        log_message(
            'No concert records found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return records


class OrchestraLumosOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestralumos_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    OrchestraLumosOrgCrawler().run()


if __name__ == '__main__':
    main()
