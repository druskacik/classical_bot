import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://carmelsymphony.org/'
CONCERTS_URL = f'{SOURCE_URL}concerts/'
SOURCE = 'Carmel Symphony Orchestra'
CITY = 'Carmel'
DEFAULT_VENUE = 'Payne & Mencias Palladium'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(' + '|'.join(
        ('January', 'February', 'March', 'April', 'May', 'June', 'July',
         'August', 'September', 'October', 'November', 'December')
    ) + r'),?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b',
    re.IGNORECASE,
)
COMPOUND_TIME_RE = re.compile(
    r'\b(\d{1,2}(?::\d{2})?)\s+(and|to)\s+(\d{1,2}(?::\d{2})?)\s*'
    r'([ap])\.?\s*m\.?',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?)\s*([ap])\.?\s*m\.?', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value, meridiem):
    try:
        return datetime.strptime(
            f'{value} {meridiem.upper()}M',
            '%I:%M %p' if ':' in value else '%I %p',
        ).strftime('%H:%M')
    except ValueError:
        return None


def times_from_segment(value):
    # Text after these markers belongs to ticketing or descriptive content, not
    # to the occurrence's advertised start time.
    value = re.split(
        r'\b(?:Fireworks|Buy Tickets|Music Director|Guest Conductor)\b',
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    times = []
    occupied = []
    for match in COMPOUND_TIME_RE.finditer(value):
        first, connector, second, meridiem = match.groups()
        times.append(parse_time(first, meridiem))
        if connector.lower() == 'and':
            times.append(parse_time(second, meridiem))
        occupied.append(match.span())

    for match in TIME_RE.finditer(value):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        times.append(parse_time(*match.groups()))
    return list(dict.fromkeys(time for time in times if time))


def occurrences_from_text(value):
    matches = list(DATE_RE.finditer(value))
    occurrences = []
    for index, match in enumerate(matches):
        month, day, year = match.groups()
        try:
            event_date = datetime.strptime(
                f'{month} {day} {year}', '%B %d %Y'
            ).date().isoformat()
        except ValueError:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        times = times_from_segment(value[match.end():end]) or [None]
        occurrences.extend((event_date, time_from) for time_from in times)
    return occurrences


def event_url(article):
    for link in article.select('a[href]'):
        href = link.get('href', '').strip()
        if 'thecenterpresents.org/tickets-events/events/' in href:
            return href
    return CONCERTS_URL


def parse_article(article):
    title_node = article.select_one('h2.el-title')
    content = article.select_one('.el-content')
    if not title_node or not content:
        return []

    title = clean_text(title_node.get_text(' ', strip=True))
    description = clean_text(content.get_text('\n', strip=True))
    occurrences = occurrences_from_text(description)
    if not title or not occurrences:
        return []

    venue = (
        'Gazebo at Civic Square'
        if re.search(r'Gazebo at Civic Square', description, re.IGNORECASE)
        else DEFAULT_VENUE
    )
    url = event_url(article)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': description or None,
        }
        for event_date, time_from in occurrences
    ]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(CONCERTS_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    records = []
    for article in soup.select('article.el-item'):
        records.extend(parse_article(article))

    if not records:
        log_message(
            'No concert records found',
            event='crawler_empty_listing',
            level='warning',
            url=CONCERTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class CarmelSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='carmelsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    CarmelSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
