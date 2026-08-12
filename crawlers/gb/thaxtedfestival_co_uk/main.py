import re
from datetime import date
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.thaxtedfestival.co.uk/'
EVENTS_URL = f'{SOURCE_URL}events'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Thaxted Festival'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def programme_year(session):
    soup = BeautifulSoup(get_response(session, EVENTS_URL).text, 'html.parser')
    heading = clean_text(soup.select_one('h1'))
    match = re.search(r'\b(20\d{2})\b', heading)
    if not match:
        match = re.search(r'Thaxted Festival\s+(20\d{2})\s+Events', clean_text(soup))
    if not match:
        raise ValueError('Could not determine the programme year')
    return int(match.group(1))


def event_urls(session):
    root = ElementTree.fromstring(get_response(session, SITEMAP_URL).content)
    namespace = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    prefix = f'{EVENTS_URL}/'
    return sorted({
        node.text.strip()
        for node in root.findall('.//sm:loc', namespace)
        if node.text and node.text.strip().startswith(prefix)
    })


def parse_date_line(text, year):
    match = re.search(
        r'(?m)^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+'
        r'(\d{1,2})\s+([A-Za-z]+),?\s+(.+)$',
        text,
    )
    if not match:
        return None, []
    month = MONTHS.get(match.group(2).lower())
    if month is None:
        return None, []
    try:
        event_date = date(year, month, int(match.group(1))).isoformat()
    except ValueError:
        return None, []

    time_matches = re.findall(
        r'\b(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm|noon)\b',
        match.group(3),
        flags=re.IGNORECASE,
    )
    times = []
    for hour_text, minute_text, period in time_matches:
        hour = int(hour_text)
        minute = int(minute_text or '00')
        period = period.lower()
        if period == 'pm' and hour != 12:
            hour += 12
        elif period == 'am' and hour == 12:
            hour = 0
        elif period == 'noon':
            hour = 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            times.append(f'{hour:02d}:{minute:02d}')

    # A range is one occurrence; ampersand-separated times are distinct shows.
    if re.search(r'\bto\b', match.group(3), flags=re.IGNORECASE) and times:
        times = times[:1]
    return event_date, list(dict.fromkeys(times))


def parse_location(text):
    match = re.search(r'(?mi)^Venue:\s*(.+)$', text)
    if not match:
        if re.search(r'\bin Thaxted Parish Church\b', text, flags=re.IGNORECASE):
            return 'Thaxted Parish Church', 'Thaxted'
        return None
    supplied = match.group(1).strip().rstrip('.')
    lower = supplied.lower()
    locations = (
        ('thaxted parish church', 'Thaxted Parish Church', 'Thaxted'),
        ("john webb’s windmill", "John Webb’s Windmill", 'Thaxted'),
        ("john webb's windmill", "John Webb’s Windmill", 'Thaxted'),
        ('the fry art gallery', 'The Fry Art Gallery', 'Saffron Walden'),
        ('bolford street hall', 'Bolford Street Hall', 'Thaxted'),
        ('audley end enchanted railway', 'Audley End Enchanted Railway', 'Saffron Walden'),
        ('saffron screen', 'Saffron Screen', 'Saffron Walden'),
    )
    for prefix, venue, city in locations:
        if lower.startswith(prefix):
            return venue, city
    return None


def parse_event(html, url, year):
    soup = BeautifulSoup(html, 'html.parser')
    heading = soup.select_one('h1.heading-article')
    section = heading.find_parent(class_='section') if heading else None
    title = re.sub(r'\s+', ' ', clean_text(heading)).strip()
    description = clean_text(section)
    event_date, times = parse_date_line(description, year)
    location = parse_location(description)
    if not title or not event_date or not location:
        return []
    venue, city = location
    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': description or None,
    } for event_time in (times or [None])]


class ThaxtedFestivalCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='thaxtedfestival_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            year = programme_year(session)
            urls = event_urls(session)
        except (requests.RequestException, ElementTree.ParseError) as error:
            log_message(
                'Failed to fetch Thaxted Festival event index',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for url in urls:
            try:
                response = get_response(session, url)
                records.extend(parse_event(response.text, url, year))
            except requests.RequestException as error:
                if getattr(error.response, 'status_code', None) == 404:
                    continue
                log_message(
                    'Failed to fetch Thaxted Festival event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    ThaxtedFestivalCoUkCrawler().run()


if __name__ == '__main__':
    main()
