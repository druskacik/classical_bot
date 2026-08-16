import re
from datetime import date, datetime
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://nbsymphony.org/'
SITEMAP_URL = f'{SOURCE_URL}page-sitemap.xml'
CHAMBER_URL = f'{SOURCE_URL}chamber-music-series/'
SOURCE = 'New Bedford Symphony Orchestra'
COUNTRY_CODE = 'US'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    month: number for number, month in enumerate(
        ('January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December'),
        1,
    )
}

VENUE_CITIES = {
    'The Zeiterion': 'New Bedford',
    'Zeiterion Performing Arts Center': 'New Bedford',
    'Kilburn Mill Event Center': 'New Bedford',
    'Kilburn Event Center': 'New Bedford',
    'Bronspiegel Auditorium': 'New Bedford',
    'New Bedford High School': 'New Bedford',
}

FULL_DATE_RE = re.compile(
    r'(?:(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*)?'
    r'(?P<month>' + '|'.join(MONTHS) + r')\s+(?P<day>\d{1,2}),\s*'
    r'(?:(?P<year>20\d{2}),?\s*)?'
    r'(?P<time>\d{1,2}(?::\d{2})?(?:\s*[AP]M)?'
    r'(?:\s*(?:and|&)\s*\d{1,2}(?::\d{2})?)?\s*[AP]M)',
    re.IGNORECASE,
)
CHAMBER_DATE_RE = re.compile(
    r'(?P<month1>' + '|'.join(MONTHS) + r')\s+(?P<day1>\d{1,2})\s*&\s*'
    r'(?:(?P<month2>' + '|'.join(MONTHS) + r')\s+)?(?P<day2>\d{1,2}),\s*'
    r'(?P<year>20\d{2}),?\s*(?P<time>\d{1,2}(?::\d{2})?\s*[AP]M)',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def iso_date(year, month, day):
    try:
        return date(int(year), MONTHS[month.title()], int(day)).isoformat()
    except (KeyError, ValueError):
        return None


def iso_time(value):
    normalized = re.sub(r'\s+', ' ', value.strip().upper())
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def event_times(value):
    meridiem = re.findall(r'[AP]M', value.upper())[-1]
    values = re.findall(r'\d{1,2}(?::\d{2})?', value)
    return [converted for item in values if (converted := iso_time(f'{item} {meridiem}'))]


def season_year(url, month):
    match = re.search(r'/(20\d{2})-(20\d{2})-season/', url)
    if not match:
        return None
    return match.group(1) if MONTHS[month.title()] >= 8 else match.group(2)


def page_text(soup):
    content = soup.select_one('main, article, .et-l--body') or soup.body
    text = clean_text(content)
    for marker in ('\nYour NBSO\n', '\nAbout Us\n'):
        text = text.split(marker, 1)[0]
    return text.strip()


def sitemap_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    root = ElementTree.fromstring(response.content)
    return [node.text.strip() for node in root.findall('.//{*}loc') if node.text]


def season_page_urls(urls):
    selected = []
    for url in urls:
        path = urlparse(url).path.rstrip('/')
        if not re.search(r'/20\d{2}-20\d{2}-season/', path + '/'):
            continue
        if re.fullmatch(r'/20\d{2}-20\d{2}-season', path):
            continue
        selected.append(url)
    return sorted(set(selected))


def find_venue(text, date_match):
    nearby = text[max(0, date_match.start() - 250):date_match.end() + 2500]
    for venue, city in VENUE_CITIES.items():
        if venue.lower() in nearby.lower():
            return venue, city
    return None, None


def season_records(soup, url):
    title_node = soup.select_one('h1')
    title = re.sub(r'\s+', ' ', clean_text(title_node)).strip()
    text = page_text(soup)
    match = FULL_DATE_RE.search(text[:2500])
    if not title or not match:
        return []
    venue, city = find_venue(text, match)
    year = match.group('year') or season_year(url, match.group('month'))
    event_date = iso_date(year, match.group('month'), match.group('day')) if year else None
    times = event_times(match.group('time'))
    if not event_date or not times or not venue or not city:
        return []
    if (match.group('weekday')
            and date.fromisoformat(event_date).strftime('%A').lower()
            != match.group('weekday').lower()):
        return []
    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': COUNTRY_CODE,
        'description': text or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for time_from in times]


def chamber_records(soup, url):
    records = []
    event_headings = []
    for heading in soup.find_all('h2'):
        date_heading = heading.find_next('h3')
        if date_heading and CHAMBER_DATE_RE.search(clean_text(date_heading)):
            event_headings.append((heading, date_heading))

    for index, (heading, date_heading) in enumerate(event_headings):
        title = re.sub(r'\s+', ' ', clean_text(heading)).strip()
        match = CHAMBER_DATE_RE.search(clean_text(date_heading))
        if not title or not match:
            continue
        stop = event_headings[index + 1][0] if index + 1 < len(event_headings) else None
        parts = []
        node = heading
        while node and node is not stop:
            if getattr(node, 'name', None) in {'h2', 'h3', 'h4', 'p', 'ul'}:
                value = clean_text(node)
                if value and value not in parts:
                    parts.append(value)
            node = node.find_next()
        description = '\n'.join(parts).split('\nPurchase Tickets', 1)[0].strip()
        month2 = match.group('month2') or match.group('month1')
        occurrences = (
            (match.group('month1'), match.group('day1'),
             'St. Gabriel’s Episcopal Church', 'Marion'),
            (month2, match.group('day2'),
             'St. Peter’s Episcopal Church', 'South Dartmouth'),
        )
        for month, day, venue, city in occurrences:
            event_date = iso_date(match.group('year'), month, day)
            if not event_date:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': iso_time(match.group('time')),
                'venue': venue,
                'city': city,
                'country_code': COUNTRY_CODE,
                'description': description or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = sitemap_urls(session)
    records = []

    for url in season_page_urls(urls):
        try:
            records.extend(season_records(get_soup(session, url), url))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape NBSO season concert',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    chamber_urls = {CHAMBER_URL}
    chamber_urls.update(
        url for url in urls
        if '/chamber-music-series/' in url
        and 'template' not in url.lower()
        and url.rstrip('/') != CHAMBER_URL.rstrip('/')
    )
    for url in sorted(chamber_urls):
        try:
            records.extend(chamber_records(get_soup(session, url), url))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape NBSO chamber concert',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {(record['title'], record['date'], record['time_from'], record['venue']): record
              for record in records}
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class NbSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nbsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    return NbSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
